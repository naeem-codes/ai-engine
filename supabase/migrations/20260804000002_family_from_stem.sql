-- Rules are now keyed PER MODEL VERSION, not per family (requested 2026-08-04), so model_key
-- holds a full stem like 'KELLY-24.00X48.00-LED' instead of 'KELLY-LED' / 'KELLY-LED#2'.
--
-- The family column therefore has to derive the grouping the same way the engine does — by
-- stripping the SIZE TOKEN — rather than by splitting on '#', which for a version key returned
-- the whole stem and grouped nothing. Family grouping is what lets the engine fall back to a
-- sibling version's rules for a freshly-cloned size, and what a future "combine these versions
-- into one set" feature would group by.
--
-- Mirrors rules_store.family_from_stem():
--     KELLY-24.00X48.00-LED       -> KELLY-LED
--     KELLY-30.00X48.00-LED       -> KELLY-LED     (same family, different version)
--     ISABELL-24.00X36.00-LED-HO  -> ISABELL-LED-HO
--     CLARA-36.00X36.00           -> CLARA
--     KELLY-LED#2                 -> KELLY-LED     (legacy family-keyed rows still group)
--
-- A generated column cannot have its expression altered in place, so it is dropped and
-- re-added; the index that depends on it is rebuilt afterwards.

drop index if exists public.rules_client_family_idx;
alter table public.rules drop column if exists family;

alter table public.rules
  add column family text generated always as (
    upper(
      trim(both '-' from
        regexp_replace(
          regexp_replace(
            split_part(model_key, '#', 1),           -- legacy '#n' suffix first
            '[-_[:space:]]*[0-9]+(\.[0-9]+)?[[:space:]]*[xX][[:space:]]*[0-9]+(\.[0-9]+)?[-_[:space:]]*',
            '-', 'g'),                               -- strip the size token
          '[-_[:space:]]+', '-', 'g')                -- collapse separators
      )
    )
  ) stored;

create index if not exists rules_client_family_idx on public.rules (client_id, family);
