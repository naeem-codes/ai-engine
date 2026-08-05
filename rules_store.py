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

from log import log
from resize_policy import component_of

RULES_SUFFIX = ".rules.json"


# ── locations ─────────────────────────────────────────────────────────────────

def _bundle_dir() -> Path:
    """Folder of `ai-engine.exe` when frozen (PyInstaller), else of this source file."""
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent


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
    fam = _SIZE_RE.sub("-", _COPY_RE.sub("", stem))
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

    Order:
      1. THIS VERSION's own set (exact stem) — the normal case, since rules are saved per
         version. Used even if its coverage is imperfect: the user tuned it for this model.
      2. a SIBLING version in the same family, best dim coverage first. This is what keeps a
         freshly-cloned size working: SaveSizedVariant renames the assembly, so a new size has
         no set of its own, and without this tier it would fall through to LLM classification —
         the original bug. Dependents scale by constant offset, so a sibling's rules hold at
         another size.

    `live_dims` empty/None means "dims unknown" — the case for /get-rules, which is only
    displaying a rule set. Coverage scoring is then skipped entirely and the newest candidate
    wins, because scoring against an empty dim list would reject every set as unusable.
    """
    stem = stem_of(model_path)
    if not stem:
        return Selection()
    scoring = bool(live_dims)

    exact = read_key(stem)
    if exact is not None:
        cov = coverage(exact, live_dims) if scoring else Coverage()
        log(f"  [STORE] this version's own rule set '{stem}'"
            + (f" — {cov.present}/{cov.total} dims present" if scoring else ""))
        return Selection(doc=exact, key=stem, source="version", cover=cov, considered=1)

    family = family_of(model_path)
    keys = [k for k in candidate_keys(family) if k != stem]
    scored: list[tuple[float, float, str, dict, Coverage]] = []
    for key in keys:
        doc = read_key(key)
        if doc is None:
            continue
        cov = coverage(doc, live_dims) if scoring else Coverage()
        if scoring and not cov.usable:
            log(f"  [STORE] '{key}' rejected — master dim(s) absent: {', '.join(cov.missing_masters)}")
            continue
        mtime = _key_path(key).stat().st_mtime
        scored.append((cov.fraction, mtime, key, doc, cov))

    if not scored:
        log(f"  [STORE] no rule set for '{stem}' and no usable sibling in family '{family}' "
            f"({len(keys)} candidate(s))")
        return Selection(considered=len(keys))

    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)   # best coverage, newest breaks ties
    _frac, _mtime, key, doc, cov = scored[0]
    log(f"  [STORE] no set for '{stem}' — using SIBLING '{key}' from family '{family}'  "
        + (f"({cov.present}/{cov.total} dims, {len(scored)}/{len(keys)} usable candidate(s))"
           if scoring else f"(newest of {len(keys)} candidate(s); dims not scored)"))
    return Selection(doc=doc, key=key, source="sibling", cover=cov, considered=len(keys))


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
