-- Rules mirror for the Lumi Design AI engine.
--
-- One row per rule set. The PRIMARY KEY is the PAIR (client_id, model_key), so one client
-- holds as many rule sets as it has product families — ten rule files for one client are ten
-- rows sharing a client_id, not a conflict.
--
-- model_key is "<FAMILY>" for a family's primary set and "<FAMILY>#n" for an additional
-- bill-of-materials variant (a size band whose parts differ):
--     KELLY-LED        covers every KELLY-LED size
--     AMBER-LED#2      the 60" band, whose chassis has 8 B-slots instead of 6
--
-- Everything else about a rule set — family, variant, generated_for, the rules themselves —
-- lives inside `doc`, so there is no second copy of anything to drift. The engine computes a
-- set's dimension fingerprint from `doc` at read time.

create extension if not exists moddatetime schema extensions;

create table if not exists public.rules (
  client_id  text not null,
  model_key  text not null,
  doc        jsonb not null,
  -- Derived, never written by the client: lets the engine ask for a whole family with
  -- `family=eq.KELLY-LED` instead of a `model_key LIKE 'KELLY-LED%'` prefix match, which also
  -- matched the DIFFERENT product KELLY-LED-HO. Generated, so it cannot drift from model_key.
  family     text generated always as (split_part(model_key, '#', 1)) stored,
  updated_at timestamptz not null default now(),
  primary key (client_id, model_key)
);

-- Authoritative server-side timestamp for the engine's newest-wins comparison, so syncing
-- never depends on client machines agreeing about the time.
drop trigger if exists rules_touch on public.rules;
create trigger rules_touch
  before update on public.rules
  for each row execute function extensions.moddatetime(updated_at);

-- The engine's only read: this client's sets for one family.
create index if not exists rules_client_family_idx on public.rules (client_id, family);


-- ── Tenant isolation ─────────────────────────────────────────────────────────
-- The client_id comes from the ACCESS TOKEN, never from anything the app sends, so editing a
-- config file (or calling the REST API by hand) cannot reach another client's rules.
--
-- Today the engine ships a long-lived token minted by tools/mint_client_token.py carrying
-- app_metadata.client_id. When real logins arrive, stamp the same claim onto each Supabase
-- auth user (admin API, app_metadata) and THIS POLICY KEEPS WORKING UNCHANGED — that is the
-- reason for using a claim now instead of the quicker route of trusting a client-sent value.
alter table public.rules enable row level security;

-- Minted tokens carry role "authenticated", so privileges go there rather than loosening the
-- public anon role (whose key ships inside the build and is readable).
grant select, insert, update, delete on public.rules to authenticated;

create or replace function public.current_client_id() returns text
language sql stable as $$
  select coalesce(
    auth.jwt() -> 'app_metadata' ->> 'client_id',
    auth.jwt() ->> 'client_id',      -- tolerated fallback for a top-level claim
    ''
  );
$$;

drop policy if exists rules_tenant_rw on public.rules;
create policy rules_tenant_rw on public.rules
  for all
  to authenticated
  using      (client_id = public.current_client_id() and public.current_client_id() <> '')
  with check (client_id = public.current_client_id() and public.current_client_id() <> '');
