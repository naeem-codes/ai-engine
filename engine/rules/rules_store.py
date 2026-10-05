"""Where rules files LIVE, and which rule set applies to a given model.

Two problems this module exists to solve:

 1. STORAGE LOCATION. Rules used to be written next to `ai-engine.exe`, i.e. INSIDE the
    folder the client deletes when installing a new build — so every upgrade wiped them and
    forced a full regeneration. They now live in a per-machine data directory
    (`%PROGRAMDATA%\\LumiDesignAI\\rules`) that no build ever touches. `rules-seed/` in the
    distribution holds factory defaults which are copied in ONLY when nothing exists for
    that key, so a new build can ship a baseline without clobbering tuned rules.

 2. ONE RULE SET PER MODEL VERSION, WITH A SIBLING FALLBACK. Rules are saved against the full
    model stem (`KELLY-24.00X48.00-LED`), so every version of a mirror can be tuned
    independently — requested 2026-08-04, after an earlier scheme that shared one set across a
    whole family.

    Sharing is still what makes a NEW size work, though, so it survives as a fallback rather
    than as the storage key. A rules file names dims like `D3@Sketch106 [12204-CHASSIS-1]`, and
    the size token never appears in a dim name — SaveSizedVariant renames only size-BEARING
    files, so component ids (and therefore dim names) are identical at 24x48 and 30x48. Without
    the fallback a freshly-cloned size would have no rules at all and drop to the weaker LLM
    classification path, which is the bug that started all this. So:

        this version's own set  ->  best-covering sibling in the family  ->  nothing

    `family_of` derives the grouping (`KELLY-24.00X48.00-LED` -> `KELLY-LED`), and siblings are
    ranked by DIM COVERAGE against the live model, so a part swap (AMBER 36 vs 60 has a
    different chassis part number and different dim names) is rejected rather than misapplied.
    Combining versions into one shared set later is a matter of grouping by `family`.
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from engine.core.log import log
from engine.resize.resize_policy import component_of

RULES_SUFFIX = ".rules.json"


# ── locations ─────────────────────────────────────────────────────────────────

def _bundle_dir() -> Path:
    """Folder of `ai-engine.exe` when frozen (PyInstaller), else the engine root (this file is
    engine/rules/rules_store.py, so the root is parents[2])."""
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """The LIVE rules location — deliberately outside any shipped folder.

    `LUMI_DATA_DIR` overrides it (used by the tests, and handy for a portable install).
    """
    root = os.getenv("LUMI_DATA_DIR")
    base = Path(root) if root else Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "LumiDesignAI"
    d = base / "rules"
    d.mkdir(parents=True, exist_ok=True)
    return d


def seed_dir() -> Path:
    """Read-only factory defaults shipped with the build (never written to)."""
    return _bundle_dir() / "rules-seed"


def legacy_dir() -> Path:
    """The pre-move location, still read once so existing installs migrate themselves."""
    return _bundle_dir() / "rules"


def outbox_dir() -> Path:
    """Pending cloud pushes, kept so an offline save is not lost."""
    d = data_dir().parent / "outbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── family / key derivation ───────────────────────────────────────────────────

# A size token in a file stem: "24.00X48.00", "36 X 36", "30x40". Everything else in the
# stem (product name AND suffix) identifies the product, so `-LED` and `-LED-HO` never
# collapse into one family.
_SIZE_RE = re.compile(r"[-_\s]*\d+(?:\.\d+)?\s*[xX]\s*\d+(?:\.\d+)?[-_\s]*")

# A LONE size token, which is how a ROUND product names itself: one diameter, not a pair.
# "ECLIPSE-30.00-LED" -> "ECLIPSE-LED". Without this every diameter was its own family, so a
# round product needed its rules regenerated at each size and lost them on a variant save --
# the exact failure that keying by family fixed for rectangular products.
#
# Requires a DECIMAL POINT, deliberately. A bare integer is far too easy to hit inside a product
# name, while every round stem the client writes carries two decimals exactly as the WxH stems do
# ("ECLIPSE-30.00-LED", never "ECLIPSE-30-LED"). Only tried when the WxH pattern did NOT match,
# so a rectangular stem can never reach it.
_DIA_RE = re.compile(r"[-_\s]+\d+\.\d+(?=[-_\s]|$)")
_COPY_RE = re.compile(r"_COPY$", re.IGNORECASE)
_KEY_RE = re.compile(r"^(?P<family>.+?)(?:#(?P<n>\d+))?$")


def stem_of(model_path: str | None) -> str:
    """File stem with any Work-on-a-Copy suffix removed."""
    if not model_path:
        return ""
    return _COPY_RE.sub("", Path(model_path).stem)


def family_from_stem(stem: str) -> str:
    """Family key for an already-extensionless stem.

    Separate from `family_of` on purpose: these stems contain dots as part of the SIZE
    ("KELLY-24.00X48.00-LED"), so running them through `Path.stem` would chop at the last dot
    and yield "KELLY-24.00X48" → family "KELLY". Only a real path may be given to Path.stem.
    """
    if not stem:
        return ""
    stripped = _COPY_RE.sub("", stem)
    fam = _SIZE_RE.sub("-", stripped)
    if fam == stripped:
        fam = _DIA_RE.sub("-", fam, count=1)
    return re.sub(r"[-_\s]+", "-", fam).strip("-").upper()


def family_of(model_path: str | None) -> str:
    """Product family key for a model PATH: the file stem minus its size token.

        KELLY-24.00X48.00-LED.SLDASM       -> KELLY-LED
        ISABELL-24.00X36.00-LED-HO.SLDASM  -> ISABELL-LED-HO
        CLARA-36.00X36.00.SLDASM           -> CLARA
        TOMO-24.00X36.00- BRUSHED BRASS.SLDASM -> TOMO-BRUSHED-BRASS
    """
    return family_from_stem(stem_of(model_path))


def _key_path(key: str) -> Path:
    return data_dir() / f"{key}{RULES_SUFFIX}"


def read_key(key: str) -> dict | None:
    path = _key_path(key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"  [STORE] unreadable {path.name}: {exc}")
        return None


def write_key(key: str, doc: dict) -> Path:
    path = _key_path(key)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def candidate_keys(family: str) -> list[str]:
    """Every stored key in `family` — its versions, plus legacy `family` / `family#n` keys.

    Grouping runs each key through `family_from_stem`, not a `#` split: keys are now per
    version (`KELLY-24.00X48.00-LED`), so the family has to be derived by stripping the size
    token exactly as it is for a model path. A `#` suffix is dropped first so rule sets written
    by the earlier family-keyed scheme still group correctly.
    """
    if not family:
        return []
    out: list[str] = []
    for p in sorted(data_dir().glob(f"*{RULES_SUFFIX}")):
        key = p.name[: -len(RULES_SUFFIX)]
        if family_from_stem(key.split("#")[0]).upper() == family.upper():
            out.append(key)
    return out


def next_key(family: str) -> str:
    """The family key if free, else the next unused `family#n`."""
    if not _key_path(family).exists():
        return family
    n = 2
    while _key_path(f"{family}#{n}").exists():
        n += 1
    return f"{family}#{n}"


# ── fingerprint / coverage ────────────────────────────────────────────────────

def dims_of(doc: dict) -> set[str]:
    """Every dim name a rule set REFERENCES.

    Computed from the doc rather than stored beside it: a cached copy can drift out of sync
    with the rules it claims to describe, and these docs are small enough (largest in the
    wild: 6 KB) that recomputing costs nothing.
    """
    names: set[str] = set()
    for axis in ("width", "height"):
        for pair in doc.get(axis, []) or []:
            if pair.get("if_changes"):
                names.add(pair["if_changes"])
            names.update(d for d in pair.get("also_change", []) or [] if d)
    for r in doc.get("offset", []) or []:
        names.update(x for x in (r.get("target_dim"), r.get("source_dim")) if x)
    for r in doc.get("position", []) or []:
        names.update(x for x in (r.get("position_dim"), r.get("driver_dim")) if x)
    return names


def masters_of(doc: dict) -> set[str]:
    """The `if_changes` dims — a rule set is useless if these are absent from the model."""
    return {p["if_changes"] for axis in ("width", "height")
            for p in doc.get(axis, []) or [] if p.get("if_changes")}


def component_ids_of(names) -> set[str]:
    """Component ids referenced by a set of dim names (empty for a single-part model)."""
    return {c for c in (component_of(n) for n in names) if c}


@dataclass
class Coverage:
    total: int = 0
    present: int = 0
    missing: list[str] = field(default_factory=list)
    missing_masters: list[str] = field(default_factory=list)

    @property
    def fraction(self) -> float:
        return self.present / self.total if self.total else 0.0

    @property
    def usable(self) -> bool:
        """A set is usable when every master exists in the live model.

        Missing DEPENDENTS are tolerated (the rules UI lets the user trim deps deliberately),
        but a missing master means this set was generated for a different configuration —
        applying it would resize nothing while looking successful.
        """
        return self.total > 0 and not self.missing_masters


def coverage(doc: dict, live_dims) -> Coverage:
    live = set(live_dims or [])
    referenced = dims_of(doc)
    missing = sorted(referenced - live)
    return Coverage(
        total=len(referenced),
        present=len(referenced & live),
        missing=missing,
        missing_masters=sorted(masters_of(doc) - live),
    )


# ── selection ─────────────────────────────────────────────────────────────────

@dataclass
class Selection:
    doc: dict | None = None
    key: str = ""
    source: str = "none"          # "version" | "sibling" | "none"
    cover: Coverage = field(default_factory=Coverage)
    considered: int = 0

    @property
    def warning(self) -> str:
        """Clauses for the chat explanation, or "" when this version's own set matched cleanly.

        Surfacing this matters: the old failure mode for a mismatched rules file was
        SILENT — unknown deps were skipped with only an engine-log line, so the frame
        under-grew and looked like a resize bug.
        """
        if self.doc is None:
            return ""
        notes = []
        if self.source == "sibling":
            # Borrowed rules are usually right (constant-offset dependents hold at any size),
            # but the user should know they can tune this version separately.
            notes.append(f" This model has no rule set of its own, so rules from "
                         f"'{self.key}' were used. Generate Rules to give this version its own.")
        if self.cover.missing:
            shown = ", ".join(self.cover.missing[:3])
            more = f" (+{len(self.cover.missing) - 3} more)" if len(self.cover.missing) > 3 else ""
            notes.append(f" Note: rule set '{self.key}' references "
                         f"{len(self.cover.missing)} dimension(s) not present in this model — "
                         f"{shown}{more} — so they were left unchanged.")
        return "".join(notes)


def select_for_model(model_path: str | None, live_dims=None) -> Selection:
    """Pick the rule set that best fits the model now open.

    Best DIM COVERAGE against the live model wins, and THIS VERSION's own set (exact stem) wins
    every tie. So in the normal case — nothing renamed, its own set at 100% — it is chosen exactly
    as before, and "the user tuned it for this model" still holds wherever the fit is equal.

    A sibling only wins by describing the open model strictly better. That is what makes a fork
    take over after a rename: renaming a part renames its component instance, every dim the
    original set names by that instance stops existing, and the fork written alongside it names
    the new ones. Live 2026-08-21: the exact-stem set was returned at **0/8 dims present** while
    `CLARA-36.00X36.00#3` sat unused at 8/8 — the chassis never resized, the hanger cap silently
    switched off for want of a chassis width, and the tab follower then wrote a 35.75" spacing
    into a 30" chassis and aborted the rebuild.

    Checking only `usable` (masters present) would not be enough: renaming a DEPENDENT leaves the
    masters intact, so the stale set would still have won — and that is the common case.

    The sibling tier also keeps a freshly-cloned size working: SaveSizedVariant renames the
    assembly, so a new size has no set of its own. Dependents scale by constant offset, so a
    sibling's rules hold at another size.

    `live_dims` empty/None means "dims unknown" — the case for /get-rules, which is only
    displaying a rule set. Coverage scoring is then skipped entirely and the newest candidate
    wins, because scoring against an empty dim list would reject every set as unusable.
    """
    stem = stem_of(model_path)
    if not stem:
        return Selection()
    scoring = bool(live_dims)

    exact = read_key(stem)
    cov_exact = Coverage()
    if exact is not None:
        cov_exact = coverage(exact, live_dims) if scoring else Coverage()
        # Not scoring means the caller only wants to SHOW a rule set (/get-rules). Scoring an
        # empty dim list would reject everything, so the model's own set is simply returned.
        if not scoring:
            log(f"  [STORE] this version's own rule set '{stem}' (dims not scored)")
            return Selection(doc=exact, key=stem, source="version", cover=cov_exact, considered=1)
        log(f"  [STORE] this version's own rule set '{stem}' — "
            f"{cov_exact.present}/{cov_exact.total} dims present")

    family = family_of(model_path)
    keys = [k for k in candidate_keys(family) if k != stem]
    scored: list[tuple[float, float, float, str, dict, Coverage]] = []
    for key in keys:
        doc = read_key(key)
        if doc is None:
            continue
        cov = coverage(doc, live_dims) if scoring else Coverage()
        # A missing MASTER used to reject the set outright. It no longer does, because the anchor
        # is the one part of a rule set the app can always replace: it measures the master off the
        # model every Refresh. The DEPENDANTS are the hard part, and a set that names them
        # correctly is worth having whatever its trigger says.
        #
        # HALO, live 2026-09-15. The `#2` fork covered 6 of 7 dims and was thrown away because its
        # trigger still read `HALO-60-MIRROR` after the mirror had become `HALO-70-MIRROR`; the
        # exact-stem set was kept at **0 of 7**, every dependant evaporated, the mirror shrank
        # alone and the crossing check refused the resize. Ranked below an anchorable set of equal
        # coverage, never above it.
        if scoring and not cov.usable:
            # …but only when its DEPENDANTS are actually here. A set with nothing present is not
            # describing this model at all, anchor or no anchor, and is still refused.
            if cov.present == 0:
                log(f"  [STORE] '{key}' rejected — nothing it names exists here "
                    f"(0/{cov.total} dims, master(s) {', '.join(cov.missing_masters)})")
                continue
            log(f"  [STORE] '{key}' names a master this model does not have "
                f"({', '.join(cov.missing_masters)}) — still a candidate on its dependants "
                f"({cov.present}/{cov.total}); the anchor is re-derived from the model")
        mtime = _key_path(key).stat().st_mtime
        scored.append((1.0 if cov.usable else 0.0, cov.fraction, mtime, key, doc, cov))

    if not scored:
        if exact is not None:
            log(f"  [STORE] keeping '{stem}' — no usable sibling in family '{family}' "
                f"({len(keys)} candidate(s))")
            return Selection(doc=exact, key=stem, source="version", cover=cov_exact,
                             considered=len(keys) + 1)
        log(f"  [STORE] no rule set for '{stem}' and no usable sibling in family '{family}' "
            f"({len(keys)} candidate(s))")
        return Selection(considered=len(keys))

    # Anchorable first, then coverage, then newest. So a set that still names its own master
    # wins every tie against one that does not, and only better DEPENDANT coverage promotes an
    # unanchorable fork above it.
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    _anchorable, frac, _mtime, key, doc, cov = scored[0]

    # The exact-stem set wins every TIE, so a sibling has to be strictly better to displace it.
    # Compared on the fraction rather than the raw count deliberately: after a rename a fork
    # carries the same dims as its parent, so the two are directly comparable, and the fraction
    # says "how much of what this set names actually exists".
    if exact is not None and frac <= cov_exact.fraction:
        log(f"  [STORE] keeping '{stem}' ({cov_exact.present}/{cov_exact.total}) — best sibling "
            f"'{key}' covers no better ({cov.present}/{cov.total})")
        return Selection(doc=exact, key=stem, source="version", cover=cov_exact,
                         considered=len(keys) + 1)

    if exact is not None:
        log(f"  [STORE] SIBLING '{key}' ({cov.present}/{cov.total} dims) beats this version's own "
            f"'{stem}' ({cov_exact.present}/{cov_exact.total}) — using it. The renamed model is "
            f"described by the fork, not by the set it was forked from.")
    else:
        log(f"  [STORE] no set for '{stem}' — using SIBLING '{key}' from family '{family}'  "
            + (f"({cov.present}/{cov.total} dims, {len(scored)}/{len(keys)} usable candidate(s))"
               if scoring else f"(newest of {len(keys)} candidate(s); dims not scored)"))
    return Selection(doc=doc, key=key, source="sibling", cover=cov,
                     considered=len(keys) + (1 if exact is not None else 0))


# ── saving ────────────────────────────────────────────────────────────────────

def save_for_model(model_path: str | None, doc: dict) -> str:
    """Write a rule set for THIS MODEL VERSION and return the key it was stored under.

    Keyed by the full model stem (`KELLY-24.00X48.00-LED`), not the family, so every version
    of a mirror carries its own independently-tunable rules — requested 2026-08-04. Saving is
    therefore always an update-in-place for that version and never has to guess whether two
    models share a bill of materials.

    Sibling versions remain discoverable through `family` (see `select_for_model`), which is
    what a future "combine these versions into one set" feature would group by.
    """
    stem = stem_of(model_path)
    family = family_of(model_path)
    key = stem or family

    doc = dict(doc)
    doc["model"] = stem or doc.get("model", "")
    doc["family"] = family
    doc["variant"] = stem            # this version IS the variant now
    doc["generated_for"] = stem
    path = write_key(key, doc)
    log(f"  [STORE] saved '{key}' ({path}) — version-specific, family={family}")
    return key


def read_for_key_or_family(model_path: str | None) -> dict | None:
    """Existing doc a save would build on — used to preserve sections the form didn't edit.

    This version's own set first; failing that a sibling, so the FIRST save for a new size
    inherits the family's pattern_rules / offset / position links instead of losing them.
    """
    stem = stem_of(model_path)
    exact = read_key(stem)
    if exact is not None:
        return exact
    keys = [k for k in candidate_keys(family_of(model_path)) if k != stem]
    return read_key(keys[0]) if keys else None


# ── bootstrap: migrate the old location, then seed factory defaults ───────────

def _import_file(src: Path, why: str) -> str | None:
    """Copy one rules file into the data dir under its FAMILY key, never overwriting.

    Legacy files are named per size (`KELLY-24.00X48.00-LED.rules.json`); re-keying them to
    the family on import is what makes existing rules apply to every size immediately. A
    second legacy file for the same family (a real size band) lands on `family#n`.
    """
    try:
        doc = json.loads(src.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"  [STORE] {why}: skipped {src.name} — unreadable ({exc})")
        return None
    if "width" not in doc and "height" not in doc:
        log(f"  [STORE] {why}: skipped {src.name} — pre-family schema with no width/height")
        return None

    src_stem = src.name[: -len(RULES_SUFFIX)]
    family = family_from_stem(src_stem)
    ids = component_ids_of(dims_of(doc))
    for existing_key in candidate_keys(family):
        existing = read_key(existing_key)
        if existing and component_ids_of(dims_of(existing)) == ids:
            return None              # already have this bill of materials — leave it alone
    key = next_key(family)
    doc.setdefault("family", family)
    doc.setdefault("generated_for", src_stem)
    m = _KEY_RE.match(key)
    doc.setdefault("variant", f"#{m.group('n')}" if m and m.group("n") else "default")
    write_key(key, doc)
    log(f"  [STORE] {why}: imported {src.name} → '{key}'")
    return key


def bootstrap() -> dict:
    """Migrate the legacy location, then apply factory seeds. Safe to run every startup.

    Order matters: a client's own rules (legacy dir) are imported BEFORE the shipped seeds,
    so a seed can never displace something the client generated themselves.
    """
    migrated = [k for p in sorted(legacy_dir().glob(f"*{RULES_SUFFIX}"))
                if (k := _import_file(p, "migrate")) is not None]
    seeded = [k for p in sorted(seed_dir().glob(f"*{RULES_SUFFIX}"))
              if (k := _import_file(p, "seed")) is not None]
    total = len(list(data_dir().glob(f"*{RULES_SUFFIX}")))
    if migrated or seeded:
        log(f"  [STORE] bootstrap: migrated={len(migrated)} seeded={len(seeded)} total={total}")
    return {"migrated": migrated, "seeded": seeded, "total": total, "data_dir": str(data_dir())}


# ── forking a rule set after a component rename ───────────────────────────────

# The single working rule set per model. Not a counter: one file, rewritten on every rename.
WORKING_SUFFIX = "#2"


def fork_with_renamed_components(model_path: str | None,
                                 stem_map: dict[str, str]) -> tuple[str, dict] | None:
    """Copy this model's rule set with component ids substituted, under a NEW key.

    Renaming a part renames its component instance, and the rules name every dim by that
    instance (`D1@Sketch1 [12393-CHASSIS-2]`). So after a rename the set no longer describes the
    model: a renamed DEPENDENT quietly stops resizing, and a renamed MASTER makes the whole set
    unusable — `select_for_model` rejects a set whose master dim is absent.

    ONE working file per model, updated in place. The first rename copies the original to
    `<stem>#2`; every rename after that rewrites `#2` itself, so `#3`, `#4`, ... are never
    created. Two reasons that is not just tidiness:

      * Chaining. A second rename has to build on the names the FIRST one produced. Forking from
        the original again would apply only the newest substitution and silently drop the earlier
        ones, leaving a set that describes neither the old model nor the new one.
      * Accumulation. Names change on essentially every resize, so a file per rename is a file
        per resize, for ever.

    The original is still never touched — it describes every un-renamed assembly built from the
    same product, and overwriting it would break those to fix this one.

    Nothing has to be told which set to use afterwards: `select_for_model` picks by dim coverage
    against the live model, so the fork wins as soon as the original's ids are the ones missing.

    Keyed on FILE STEMS, not component instance ids. One file can hold several instances --
    renaming `Zortech-Low-Profile-1900-Lumens` renames all four LED strips -- so an id-keyed map
    caught only one of them and left the rest naming a component that no longer exists. A nested
    component's instance name is also a slashed path
    (`12392-CHASSIS-ASSY-1/6666-CXP-CUSTOM-CHAS-2`) which no dim name carries. Substituting
    `[<oldstem>-` -> `[<newstem>-` avoids both (live 2026-08-21).

    Returns (key, doc), or None when there is nothing to fork or nothing would change.
    """
    if not stem_map:
        return None

    stem = stem_of(model_path) or family_of(model_path)
    if not stem:
        return None
    key = f"{stem}{WORKING_SUFFIX}"

    # Base on the WORKING set when there is one, so each rename builds on the last. Reading it
    # directly rather than through select_for_model: that helper is coverage-driven and gets no
    # live dims here, so it would hand back the exact-stem original and undo every earlier rename.
    doc = read_key(key)
    origin = key
    if doc is None:
        sel = select_for_model(model_path)
        doc = getattr(sel, "doc", None)
        origin = sel.key
    if not isinstance(doc, dict):
        return None

    # Anchored on the opening bracket AND on the instance number that closes the id, so it
    # matches every instance of the file and nothing that merely contains the stem as a prefix.
    #
    # The instance number is what makes this IDEMPOTENT, and idempotence is the whole point.
    # A bare `[{old}-` -> `[{new}-` replace is correct exactly once. But this function reads the
    # WORKING set so renames chain, and a rename usually APPENDS ("12393-CHASSIS" ->
    # "12393-CHASSIS-KUCHU-PUCHU", "…-REV-B", any suffix at all) — so the already-renamed
    # `[12393-CHASSIS-KUCHU-PUCHU-2]` still starts with `[12393-CHASSIS-` and got renamed a
    # SECOND time into `[12393-CHASSIS-KUCHU-PUCHU-KUCHU-PUCHU-2]`. Requiring `\d+]` after the
    # separator refuses that: "KUCHU-PUCHU-2" is not an instance number.
    #
    # Live 2026-08-25: it cost the chassis WIDTH dim, which then never resized. The hanger cap
    # reads that same dim and silently switched off, a 20" prefab was chosen for a 10" chassis,
    # and the tab follower wrote a 15.75" spacing into it — `HANGING TAB LOCATIONS` failed,
    # Sketch82 lost the geometry it was drawn on, and the resize aborted.
    subs = {re.compile(r"\[" + re.escape(old) + r"-(?=\d+\])"): f"[{new}-"
            for old, new in stem_map.items() if old and new and old != new}
    if not subs:
        return None

    def swap(text: str) -> str:
        for rx, b in subs.items():
            text = rx.sub(b, text)
        return text

    def walk(node):
        if isinstance(node, str):
            return swap(node)
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, dict):
            # KEYS too: component_labels is keyed by the bare component id, no brackets -- so it
            # needs a PREFIX swap ("12393-CHASSIS-2" -> "6666-CXP-CUSTOM-CHAS-2") rather than the
            # bracketed one the dim names use. Same instance-number guard as `subs` above, for
            # the same reason: without it "12393-CHASSIS-KUCHU-PUCHU-2" starts with
            # "12393-CHASSIS-" and gets renamed a second time on the next rename.
            out = {}
            for k, v in node.items():
                nk = k
                if isinstance(k, str):
                    for old, new in stem_map.items():
                        if (old and new and old != new and k.startswith(f"{old}-")
                                and k[len(old) + 1:].isdigit()):
                            nk = new + k[len(old):]
                            break
                out[nk] = walk(v)
            return out
        return node

    forked = walk(json.loads(json.dumps(doc)))
    if forked == doc:
        return None                       # the rename touched nothing this set references

    forked["model"] = stem
    forked["family"] = family_of(model_path)
    forked["variant"] = WORKING_SUFFIX
    forked["generated_for"] = stem
    forked["forked_from"] = stem
    write_key(key, forked)
    what = "updated" if origin == key else f"created from '{origin}'"
    log(f"  [STORE] working rule set '{key}' {what}; renamed "
        + ", ".join(f"{a}→{b}" for a, b in stem_map.items() if a != b))
    return key, forked


def delete_key(key: str) -> bool:
    """Remove one stored rule set. Returns False when there was nothing there."""
    p = _key_path(key)
    try:
        if not p.exists():
            return False
        p.unlink()
        return True
    except OSError as exc:
        log(f"  [STORE] could not delete '{key}': {exc}")
        return False


def finalize_working_set(model_path: str | None) -> tuple[str, str, str] | None:
    """Rename the working rule set to the model's CURRENT size, once a variant is finished.

    Through the whole resize / rename / build / review cycle the working set is `<old>#2` —
    deliberately, so nothing is renamed while the result might still be discarded. Export is the
    point the variant becomes real: the PDFs and DXFs are written, the folder, the assembly, the
    drawings and the outputs all carry the new size. The rules should say the same thing rather
    than keep naming a size that no longer exists anywhere on disk.

    Rekeying also upgrades the match from a heuristic to an exact one. Until now the set was
    found as a SIBLING on best coverage; afterwards it is the model's own `<stem>` key.

    The source is located by its `#2` suffix within the family, not passed in, because by the time
    Export runs the app no longer knows the stem the fork was made under — `SaveSizedVariant`
    renamed the assembly out from under it.

    Returns (old_key, new_key, note) or None when there is nothing to do. Refuses — returning a
    note rather than acting — when the target key already exists: that would be a previous
    variant's tuned rules, and silently overwriting them is worse than leaving `#2` in place.
    """
    stem = stem_of(model_path)
    if not stem:
        return None
    family = family_of(model_path)

    working = [k for k in candidate_keys(family) if k.endswith(WORKING_SUFFIX)]
    if not working:
        return None                       # nothing was ever forked, or it is already finalised
    old_key = working[0]
    if old_key == stem:
        return None                       # would be a no-op

    doc = read_key(old_key)
    if doc is None:
        return None

    if _key_path(stem).exists():
        note = (f"'{stem}' already has its own rule set — keeping '{old_key}' rather than "
                f"overwriting rules that were tuned for an earlier variant of this size")
        log(f"  [STORE] finalise REFUSED: {note}")
        return old_key, old_key, note

    doc = dict(doc)
    doc["model"] = stem
    doc["family"] = family
    doc["variant"] = stem
    doc["generated_for"] = stem
    doc.pop("forked_from", None)          # it is no longer a fork of anything; it IS the set
    write_key(stem, doc)
    delete_key(old_key)
    note = f"renamed '{old_key}' → '{stem}' to match the exported size"
    log(f"  [STORE] finalise: {note}")
    return old_key, stem, note
