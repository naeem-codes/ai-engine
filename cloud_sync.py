"""Optional cloud mirror for rules files (Supabase REST, two-way, newest-wins).

Design constraints, in priority order:

 1. NEVER BLOCK A RESIZE. `/interpret` reads the local data dir only. Syncing happens on
    `/get-rules` — which the app calls on every Refresh, i.e. immediately before any prompt —
    and on `/save-rules`. If the network is down, every failure degrades to "carry on with the
    local cache", which is exactly today's behaviour.

 2. OPT-IN. `RULES_REMOTE` defaults to "none", so an install with no cloud config behaves
    like the local-only engine. This matters beyond convenience: the roadmap's Part 5 is
    fully-local operation because the client's #1 concern is that nothing about their product
    models leaves the machine, and a rules file carries their part numbers, dim names and
    size limits.

 3. NO NEW BUNDLE WEIGHT. Plain httpx against the REST endpoint rather than a Supabase SDK or
    a Postgres driver, so PyInstaller's hidden-import list (see ai-engine.spec) stays as it is.
    Using a driver would also mean a DATABASE credential on a client machine; the REST route
    needs only the public anon key plus a token scoped to that client's own rows.

Newest-wins uses the DB's own `updated_at`, stamped into the local doc on every successful
pull/push, so comparison never depends on client clocks agreeing.

Schema and the RLS policy: supabase/migrations/20260804000001_create_rules.sql. The per-client
Auth user (and its app_metadata.client_id claim) is created by tools/create_client_user.py.
"""

import base64
import json
import os
import time
from pathlib import Path
from typing import NamedTuple

import httpx

import rules_store as store
from log import log

TIMEOUT = httpx.Timeout(8.0, connect=4.0)
TABLE = "rules"


class Config(NamedTuple):
    url: str
    key: str          # public anon key — identifies the project, safe to ship
    email: str        # per-client Auth user
    password: str
    static_token: str # optional pre-issued token, used instead of signing in


def _cfg() -> Config | None:
    """Sync configuration, or None when it is not fully set up (the default).

    Credentials and what each is for:

      * ANON KEY — the project's public identifier. Ships in every Supabase web app's
        JavaScript and grants nothing on its own; it goes in the `apikey` header.
      * EMAIL + PASSWORD — a per-client Auth user. The engine signs in and uses the access
        token Supabase issues as the bearer. This project signs tokens with an ASYMMETRIC key
        (ES256) whose private half is not exportable, so a locally minted HS256 token is
        rejected with "no suitable key or wrong key type" — only Supabase can issue an
        acceptable one. The user carries `app_metadata.client_id`, which Supabase copies into
        every token, and that is what the RLS policy enforces.
      * SUPABASE_TOKEN — optional escape hatch: a pre-issued token used verbatim instead of
        signing in (handy for a dev smoke test with a service key, or a project still on the
        legacy shared secret). Never ship a service key to a client: it bypasses RLS.
    """
    # Sync switches on when the configuration is COMPLETE — no separate flag to remember.
    # `RULES_REMOTE=none` remains an explicit kill switch (fully-local operation for a client
    # who requires it, or while testing) and wins over any credentials present. The previous
    # opt-in flag had the opposite failure mode: complete credentials plus a forgotten flag
    # meant sync was silently off with nothing to explain why.
    remote = os.getenv("RULES_REMOTE", "").strip().lower()
    if remote in ("none", "off", "local", "0", "false"):
        return None

    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = os.getenv("SUPABASE_KEY") or ""
    email = os.getenv("SUPABASE_EMAIL") or ""
    password = os.getenv("SUPABASE_PASSWORD") or ""
    static_token = os.getenv("SUPABASE_TOKEN") or ""

    missing = [n for n, v in (("SUPABASE_URL", url), ("SUPABASE_KEY", key)) if not v]
    if not static_token and not (email and password):
        missing.append("SUPABASE_EMAIL+SUPABASE_PASSWORD (or SUPABASE_TOKEN)")
    if missing:
        # Only worth a log line when the operator clearly INTENDED sync; a bare install with
        # no Supabase settings at all is the normal local-only case, not a misconfiguration.
        if remote or url or key or email or static_token:
            log(f"  [SYNC] not enabled — missing {', '.join(missing)}")
        return None
    return Config(url, key, email, password, static_token)


def _client_id_from_token(token: str) -> str:
    """Read app_metadata.client_id out of an access token, so it need not be configured twice.

    Decode only, never verify — the signature is the DATABASE's business, and trusting this
    value locally is harmless: it only decides which rows we ASK for. RLS decides what we get.
    """
    try:
        payload = token.split(".")[1]
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        data = json.loads(raw)
    except (IndexError, ValueError, json.JSONDecodeError):
        return ""
    meta = data.get("app_metadata")
    if isinstance(meta, dict) and meta.get("client_id"):
        return str(meta["client_id"])
    return str(data.get("client_id") or "")


def enabled() -> bool:
    return _cfg() is not None


# ── session (in memory only) ──────────────────────────────────────────────────
# Not persisted: a refresh token on disk buys nothing here, because the engine holds the
# password anyway and signing in again costs one request per process start. Keeping it in
# memory also means nothing extra to protect on the client machine.
_session: dict = {"access_token": "", "expires_at": 0.0, "client_id": ""}

# Re-authenticate slightly early rather than discovering expiry mid-request.
_EXPIRY_MARGIN_S = 60


def _reset_session() -> None:
    _session.update({"access_token": "", "expires_at": 0.0, "client_id": ""})


async def _sign_in(cfg: Config) -> str:
    """Exchange the client's email+password for an access token. "" on failure.

    Never raises: an unreachable or misconfigured auth endpoint must degrade to local-only,
    exactly like any other sync failure.
    """
    # Credentials WIN over a static token when both are present: a stale SUPABASE_TOKEN left
    # in a .env would otherwise shadow working credentials and fail every request with 401.
    if cfg.static_token and not (cfg.email and cfg.password):
        _session.update({"access_token": cfg.static_token, "expires_at": time.time() + 3600,
                         "client_id": os.getenv("CLIENT_ID")
                         or _client_id_from_token(cfg.static_token)})
        return cfg.static_token
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{cfg.url}/auth/v1/token", params={"grant_type": "password"},
                headers={"apikey": cfg.key, "Content-Type": "application/json"},
                json={"email": cfg.email, "password": cfg.password})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:200].replace("\n", " ")
        log(f"  [SYNC] sign-in FAILED as {cfg.email}: HTTP {exc.response.status_code} {detail}")
        return ""
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        log(f"  [SYNC] sign-in FAILED as {cfg.email}: {type(exc).__name__}: {exc}")
        return ""

    token = data.get("access_token") or ""
    if not token:
        log("  [SYNC] sign-in returned no access_token")
        return ""
    client_id = _client_id_from_token(token)
    _session.update({"access_token": token,
                     "expires_at": time.time() + float(data.get("expires_in") or 3600),
                     "client_id": client_id})
    if not client_id:
        log(f"  [SYNC] WARNING: signed in as {cfg.email} but the token carries no "
            f"app_metadata.client_id — every query will be denied by RLS. Re-run "
            f"tools/create_client_user.py to stamp the claim.")
    else:
        log(f"  [SYNC] signed in as {cfg.email} (client_id={client_id})")
    return token


async def _bearer(cfg: Config, force: bool = False) -> str:
    """A usable access token, signing in if there is none, it is stale, or `force`."""
    if not force and _session["access_token"] and time.time() < _session["expires_at"] - _EXPIRY_MARGIN_S:
        return _session["access_token"]
    return await _sign_in(cfg)


async def client_id(cfg: Config) -> str:
    """The tenant this engine acts as — from the token, falling back to explicit config."""
    if not _session["client_id"]:
        await _bearer(cfg)
    return _session["client_id"] or os.getenv("CLIENT_ID", "")


async def _request(cfg: Config, method: str, path: str, **kw) -> httpx.Response:
    """One REST call with the bearer attached, retried once after re-authenticating.

    The retry matters in normal operation: an access token lives about an hour, while the
    engine can stay open all day, so the first call after expiry would otherwise fail and the
    save would land in the outbox for no good reason.
    """
    token = await _bearer(cfg)
    if not token:
        raise httpx.HTTPError("not authenticated")
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.request(method, f"{cfg.url}{path}",
                                    headers=_headers(cfg, token, kw.pop("prefer", "")), **kw)
        if resp.status_code == 401:
            log("  [SYNC] 401 — token rejected, re-authenticating and retrying once")
            token = await _bearer(cfg, force=True)
            if not token:
                resp.raise_for_status()
            resp = await client.request(method, f"{cfg.url}{path}",
                                        headers=_headers(cfg, token, kw.pop("prefer", "")), **kw)
        resp.raise_for_status()
        return resp


def status() -> dict:
    """Non-secret summary for /sync-status — never returns the key, password or token."""
    cfg = _cfg()
    queued = len(list(store.outbox_dir().glob("*.json")))
    if cfg is None:
        remote = os.getenv("RULES_REMOTE", "").strip().lower()
        reason = ("RULES_REMOTE forces local-only" if remote in ("none", "off", "local", "0", "false")
                  else "Supabase settings incomplete (need SUPABASE_URL, SUPABASE_KEY and "
                       "SUPABASE_EMAIL+SUPABASE_PASSWORD)")
        return {"enabled": False, "reason": reason, "queued": queued}
    return {"enabled": True, "url": cfg.url,
            "auth": f"sign-in as {cfg.email}" if (cfg.email and cfg.password) else "static token",
            "client_id": _session["client_id"] or os.getenv("CLIENT_ID", "") or "(not signed in yet)",
            "signed_in": bool(_session["access_token"]), "queued": queued}


async def check() -> dict:
    """Actively verify the round trip: can we sign in and reach the table AS this client?

    Exists because the sync cannot be tested without a real project — this turns
    "is it working?" into one HTTP request instead of a failed resize.
    """
    cfg = _cfg()
    if cfg is None:
        return {"ok": False, **status()}
    if not await _bearer(cfg, force=True):
        return {"ok": False, "hint": "sign-in failed — check SUPABASE_EMAIL/PASSWORD and that "
                                     "the user exists (tools/create_client_user.py)", **status()}
    if not _session["client_id"]:
        return {"ok": False, "hint": "signed in, but the token carries no app_metadata.client_id "
                                     "— RLS will deny everything. Re-run create_client_user.py",
                **status()}
    try:
        resp = await _request(cfg, "GET", f"/rest/v1/{TABLE}",
                              params={"select": "model_key,updated_at", "limit": "5"})
        rows = resp.json()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        hint = ("the RLS policy rejected this token's client_id" if code in (401, 403) else
                "table 'rules' not found — has the migration been applied?" if code == 404 else "")
        return {"ok": False, "http": code, "body": exc.response.text[:300], "hint": hint, **status()}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", **status()}
    return {"ok": True, "rows_visible": len(rows),
            "keys": [r.get("model_key") for r in rows], **status()}


def _headers(cfg: Config, token: str, prefer: str = "") -> dict[str, str]:
    h = {"apikey": cfg.key, "Authorization": f"Bearer {token}",
         "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    return h


# ── pull ──────────────────────────────────────────────────────────────────────

async def pull_family(model_path: str | None) -> int:
    """Fetch this family's rule sets and overwrite local copies the remote has newer.

    Returns the number of local files written. Never raises — a sync failure must not stop
    the user from working off what is already on disk.
    """
    cfg = _cfg()
    if cfg is None:
        return 0
    family = store.family_of(model_path)
    if not family:
        return 0

    try:
        cid = await client_id(cfg)
        resp = await _request(
            cfg, "GET", f"/rest/v1/{TABLE}",
            # Exact match on the generated `family` column, not a `model_key LIKE`
            # prefix — the prefix form also matched the DIFFERENT product KELLY-LED-HO.
            # client_id is filtered too, though RLS already guarantees it.
            params={"client_id": f"eq.{cid}", "family": f"eq.{family}",
                    "select": "model_key,doc,updated_at"},
        )
        rows = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        log(f"  [SYNC] pull '{family}' failed ({type(exc).__name__}: {exc}) — using local cache")
        return 0

    written = 0
    for row in rows:
        model_key, doc, updated = row.get("model_key"), row.get("doc"), row.get("updated_at") or ""
        if not model_key or not isinstance(doc, dict):
            continue
        # Belt and braces: the query filters on the generated family column, so this only
        # fires if the server ever returns something unexpected.
        if model_key.split("#")[0].upper() != family.upper():
            log(f"  [SYNC] ignoring '{model_key}' — not in family '{family}'")
            continue
        local = store.read_key(model_key)
        if local is not None and (local.get("updated_at") or "") >= updated:
            continue
        doc["updated_at"] = updated
        store.write_key(model_key, doc)
        written += 1
        log(f"  [SYNC] pulled '{model_key}' (updated_at={updated})")
    if rows and not written:
        log(f"  [SYNC] '{family}': {len(rows)} remote row(s), all already current locally")
    return written


# ── push ──────────────────────────────────────────────────────────────────────

def _outbox_path(model_key: str) -> Path:
    return store.outbox_dir() / f"{model_key.replace('#', '_')}.json"


async def push(model_key: str, doc: dict) -> bool:
    """Upsert one rule set. On any failure the doc is queued for a later attempt."""
    cfg = _cfg()
    if cfg is None:
        return False
    try:
        cid = await client_id(cfg)
        if not cid:
            raise httpx.HTTPError("no client_id available (sign-in failed or claim missing)")
        body = [{"client_id": cid, "model_key": model_key, "doc": doc}]
        resp = await _request(
            cfg, "POST", f"/rest/v1/{TABLE}",
            prefer="resolution=merge-duplicates,return=representation",
            params={"on_conflict": "client_id,model_key"},
            json=body,
        )
        rows = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        _outbox_path(model_key).write_text(json.dumps(doc, indent=2), encoding="utf-8")
        log(f"  [SYNC] push '{model_key}' failed ({type(exc).__name__}: {exc}) — queued in outbox")
        return False

    # Stamp the DB's timestamp locally so the next pull compares against the server's clock.
    updated = (rows[0].get("updated_at") if rows else "") or ""
    if updated:
        stored = store.read_key(model_key) or doc
        stored["updated_at"] = updated
        store.write_key(model_key, stored)
    _outbox_path(model_key).unlink(missing_ok=True)
    log(f"  [SYNC] pushed '{model_key}' (updated_at={updated})")
    return True


async def flush_outbox() -> int:
    """Retry queued pushes (called at startup). Returns how many went through."""
    if not enabled():
        return 0
    sent = 0
    for path in sorted(store.outbox_dir().glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            path.unlink(missing_ok=True)
            continue
        # The filename is the key with '#' made filesystem-friendly; the doc knows its own.
        family = doc.get("family") or ""
        variant = doc.get("variant") or "default"
        model_key = family if variant == "default" else f"{family}{variant}"
        if await push(model_key, doc):
            sent += 1
    if sent:
        log(f"  [SYNC] flushed {sent} queued push(es)")
    return sent


async def delete(model_key: str) -> bool:
    """Remove one rule set from the cloud.

    Needed because finalising a variant REKEYS the working set: `BREAM-24.00X36.00-LED#2`
    becomes `BREAM-20.00X20.00-LED`, and without this the old row survives server-side and comes
    straight back on the next `pull_family` to a new machine — a stale set naming a size that no
    longer exists, competing for coverage against the real one.

    Best-effort, like `push`. A failure here leaves a stale ROW, never a missing one, so it must
    not turn a successful local finalise into an error the user sees. Nothing is queued: unlike a
    push there is no document to retry with, and a delete that never lands is a tidiness problem,
    not a data-loss one.
    """
    cfg = _cfg()
    if cfg is None:
        return False
    try:
        cid = await client_id(cfg)
        if not cid:
            raise httpx.HTTPError("no client_id available (sign-in failed or claim missing)")
        await _request(
            cfg, "DELETE", f"/rest/v1/{TABLE}",
            params={"client_id": f"eq.{cid}", "model_key": f"eq.{model_key}"},
        )
    except httpx.HTTPError as exc:
        log(f"  [SYNC] delete '{model_key}' failed ({type(exc).__name__}: {exc}) — "
            f"the row stays in the cloud; local state is already correct")
        return False
    _outbox_path(model_key).unlink(missing_ok=True)
    log(f"  [SYNC] deleted '{model_key}'")
    return True
