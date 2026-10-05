"""Cloud mirror behaviour, with httpx faked out.

Two rules these tests exist to protect:
  1. a sync failure must NEVER surface as a failed operation — the local data dir is
     authoritative for resizing, so an unreachable Supabase degrades to the local-only engine;
  2. the engine authenticates by SIGNING IN as a per-client Auth user, because this project
     signs tokens with an asymmetric key and rejects locally minted ones.
"""

import base64
import json

import httpx
import pytest

from engine.rules import cloud_sync
from engine.rules import rules_store as store


def _jwt(client_id: str = "acme", **extra) -> str:
    """A structurally valid access token. The signature is never checked locally — only
    Supabase verifies it — so a placeholder is honest here rather than misleading."""
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    payload = {"role": "authenticated", "app_metadata": {"client_id": client_id}}
    payload.update(extra)
    return f"{part({'alg': 'ES256', 'typ': 'JWT'})}.{part(payload)}.sig"


TOKEN = _jwt("acme")


@pytest.fixture(autouse=True)
def clean_session():
    """The token cache is module state; a leak between tests would hide sign-in bugs."""
    cloud_sync._reset_session()
    yield
    cloud_sync._reset_session()


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("RULES_REMOTE", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-anon-key")
    monkeypatch.setenv("SUPABASE_EMAIL", "acme@lumi-clients.local")
    monkeypatch.setenv("SUPABASE_PASSWORD", "hunter2-but-longer")
    monkeypatch.delenv("SUPABASE_TOKEN", raising=False)
    monkeypatch.delenv("CLIENT_ID", raising=False)


def _doc(master="W@M [MIRROR-1]", **extra):
    d = {"model": "X", "width": [{"if_changes": master, "also_change": []}], "height": []}
    d.update(extra)
    return d


class _FakeClient:
    """Async httpx.AsyncClient stand-in that routes auth calls separately from data calls.

    `data` may be a list of successive responses, so an expired-token retry can be modelled:
    each entry is either a payload (200) or an httpx.Response/Exception to raise.
    """

    def __init__(self, auth=None, data=None, raises=None):
        self._auth = auth if auth is not None else {
            "access_token": TOKEN, "expires_in": 3600, "refresh_token": "r"}
        self._data = list(data) if isinstance(data, list) else [data if data is not None else []]
        self._raises = raises
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _resp(self, payload, status=200):
        return httpx.Response(status, json=payload,
                              request=httpx.Request("GET", "https://example.supabase.co"))

    def _next_data(self):
        item = self._data.pop(0) if len(self._data) > 1 else self._data[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return self._resp(item)

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if self._raises:
            raise self._raises
        if "/auth/v1/token" in url:
            if isinstance(self._auth, Exception):
                raise self._auth
            if isinstance(self._auth, httpx.Response):
                return self._auth
            return self._resp(self._auth)
        return self._next_data()

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if self._raises:
            raise self._raises
        return self._next_data()

    async def request(self, method, url, **kw):
        return await (self.post(url, **kw) if method == "POST" else self.get(url, **kw))


def _patch(monkeypatch, fake):
    monkeypatch.setattr(cloud_sync.httpx, "AsyncClient", lambda **kw: fake)
    return fake


# ── configuration ─────────────────────────────────────────────────────────────

def test_disabled_when_nothing_is_configured(monkeypatch):
    # conftest pins RULES_REMOTE=none for the whole suite; drop it so this tests the
    # no-configuration path rather than the kill switch.
    monkeypatch.delenv("RULES_REMOTE", raising=False)
    assert cloud_sync.enabled() is False


def test_url_and_key_without_credentials_is_not_enough(monkeypatch):
    monkeypatch.delenv("RULES_REMOTE", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-anon-key")
    assert cloud_sync.enabled() is False


def test_enabled_with_email_and_password(configured):
    assert cloud_sync.enabled() is True


def test_complete_config_enables_sync_without_the_flag(configured, monkeypatch):
    """No RULES_REMOTE needed: complete credentials used to mean sync was SILENTLY off."""
    monkeypatch.delenv("RULES_REMOTE", raising=False)
    assert cloud_sync.enabled() is True


@pytest.mark.parametrize("value", ["none", "off", "local", "0", "false", "NONE"])
def test_rules_remote_still_forces_local_only(configured, monkeypatch, value):
    """The kill switch has to beat complete credentials — Part 5 clients require local-only."""
    monkeypatch.setenv("RULES_REMOTE", value)
    assert cloud_sync.enabled() is False
    assert "forces local-only" in cloud_sync.status()["reason"]


def test_status_explains_incomplete_config(monkeypatch):
    monkeypatch.delenv("RULES_REMOTE", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    assert "incomplete" in cloud_sync.status()["reason"]


def test_a_static_token_is_accepted_instead_of_credentials(monkeypatch):
    monkeypatch.setenv("RULES_REMOTE", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-anon-key")
    monkeypatch.setenv("SUPABASE_TOKEN", TOKEN)
    assert cloud_sync.enabled() is True


# ── sign-in ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_signs_in_and_takes_client_id_from_the_issued_token(configured, monkeypatch):
    fake = _patch(monkeypatch, _FakeClient())
    assert await cloud_sync.client_id(cloud_sync._cfg()) == "acme"
    method, url, kw = fake.calls[0]
    assert method == "POST" and "/auth/v1/token" in url
    assert kw["params"]["grant_type"] == "password"
    assert kw["json"]["email"] == "acme@lumi-clients.local"


@pytest.mark.asyncio
async def test_the_session_is_reused_rather_than_signing_in_per_request(configured, monkeypatch):
    fake = _patch(monkeypatch, _FakeClient(data=[[], []]))
    cfg = cloud_sync._cfg()
    await cloud_sync.pull_family("C:\\m\\KELLY-24.00X48.00-LED.SLDASM")
    await cloud_sync.pull_family("C:\\m\\AMBER-36.00X36.00-LED.SLDASM")
    assert sum(1 for c in fake.calls if "/auth/v1/token" in c[1]) == 1


@pytest.mark.asyncio
async def test_credentials_win_over_a_stale_static_token(configured, monkeypatch):
    """A dead SUPABASE_TOKEN left in a .env must not shadow working credentials."""
    monkeypatch.setenv("SUPABASE_TOKEN", "stale.dead.token")
    fake = _patch(monkeypatch, _FakeClient(data=[[]]))
    await cloud_sync.pull_family("C:\\m\\KELLY-24.00X48.00-LED.SLDASM")
    assert any("/auth/v1/token" in c[1] for c in fake.calls)          # signed in anyway
    data_call = next(c for c in fake.calls if "/rest/v1/" in c[1])
    assert data_call[2]["headers"]["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_a_static_token_skips_sign_in(monkeypatch):
    monkeypatch.setenv("RULES_REMOTE", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-anon-key")
    monkeypatch.setenv("SUPABASE_TOKEN", TOKEN)
    fake = _patch(monkeypatch, _FakeClient(data=[[]]))
    await cloud_sync.pull_family("C:\\m\\KELLY-24.00X48.00-LED.SLDASM")
    assert not any("/auth/v1/token" in c[1] for c in fake.calls)


@pytest.mark.asyncio
async def test_failed_sign_in_degrades_to_local_only(configured, monkeypatch):
    bad = httpx.Response(400, json={"error": "invalid_grant"},
                         request=httpx.Request("POST", "https://example.supabase.co"))
    _patch(monkeypatch, _FakeClient(auth=bad))
    assert await cloud_sync.pull_family("C:\\m\\KELLY-24.00X48.00-LED.SLDASM") == 0
    assert await cloud_sync.push("KELLY-LED", _doc()) is False


@pytest.mark.asyncio
async def test_a_token_without_the_claim_is_reported_not_silently_used(configured, monkeypatch):
    """RLS would deny everything; the operator needs to know why."""
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    claimless = f"{part({'alg': 'ES256'})}.{part({'role': 'authenticated'})}.sig"
    _patch(monkeypatch, _FakeClient(auth={"access_token": claimless, "expires_in": 3600}))
    res = await cloud_sync.check()
    assert res["ok"] is False
    assert "client_id" in res["hint"]


@pytest.mark.asyncio
async def test_an_expired_token_is_refreshed_and_the_call_retried(configured, monkeypatch):
    """An access token lasts ~an hour while the app can stay open all day."""
    unauthorised = httpx.Response(401, json={"message": "JWT expired"},
                                  request=httpx.Request("GET", "https://example.supabase.co"))
    fake = _patch(monkeypatch, _FakeClient(data=[
        unauthorised,
        [{"model_key": "KELLY-LED", "doc": _doc(), "updated_at": "2026-08-04T10:00:00Z"}],
    ]))
    written = await cloud_sync.pull_family("C:\\m\\KELLY-24.00X48.00-LED.SLDASM")
    assert written == 1                                        # retry succeeded
    assert sum(1 for c in fake.calls if "/auth/v1/token" in c[1]) == 2   # signed in again


# ── pull ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pull_writes_newer_remote_sets(configured, monkeypatch):
    _patch(monkeypatch, _FakeClient(data=[[
        {"model_key": "KELLY-LED", "doc": _doc(), "updated_at": "2026-08-04T10:00:00Z"},
    ]]))
    assert await cloud_sync.pull_family("C:\\m\\KELLY-24.00X48.00-LED.SLDASM") == 1
    assert store.read_key("KELLY-LED")["updated_at"] == "2026-08-04T10:00:00Z"


@pytest.mark.asyncio
async def test_pull_keeps_a_local_set_that_is_already_current(configured, monkeypatch):
    store.write_key("KELLY-LED", _doc("W@LOCAL [MIRROR-1]", updated_at="2026-08-04T12:00:00Z"))
    _patch(monkeypatch, _FakeClient(data=[[
        {"model_key": "KELLY-LED", "doc": _doc("W@REMOTE [MIRROR-1]"),
         "updated_at": "2026-08-04T09:00:00Z"},      # older than local
    ]]))
    assert await cloud_sync.pull_family("C:\\m\\KELLY-24.00X48.00-LED.SLDASM") == 0
    assert store.masters_of(store.read_key("KELLY-LED")) == {"W@LOCAL [MIRROR-1]"}


@pytest.mark.asyncio
async def test_pull_queries_the_family_column_not_a_prefix(configured, monkeypatch):
    fake = _patch(monkeypatch, _FakeClient(data=[[]]))
    await cloud_sync.pull_family("C:\\m\\KELLY-24.00X48.00-LED.SLDASM")
    data_call = next(c for c in fake.calls if "/rest/v1/" in c[1])
    assert data_call[2]["params"]["family"] == "eq.KELLY-LED"
    assert data_call[2]["params"]["client_id"] == "eq.acme"
    assert "model_key" not in data_call[2]["params"]


@pytest.mark.asyncio
async def test_pull_ignores_a_row_from_another_family(configured, monkeypatch):
    """KELLY-LED-HO is a DIFFERENT product; guards a server returning the unexpected."""
    _patch(monkeypatch, _FakeClient(data=[[
        {"model_key": "KELLY-LED-HO", "doc": _doc(), "updated_at": "2026-08-04T10:00:00Z"},
    ]]))
    assert await cloud_sync.pull_family("C:\\m\\KELLY-24.00X48.00-LED.SLDASM") == 0
    assert store.read_key("KELLY-LED-HO") is None


@pytest.mark.asyncio
async def test_pull_failure_is_swallowed(configured, monkeypatch):
    _patch(monkeypatch, _FakeClient(raises=httpx.ConnectError("no network")))
    assert await cloud_sync.pull_family("C:\\m\\KELLY-24.00X48.00-LED.SLDASM") == 0


# ── push ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_push_upserts_and_stamps_the_server_timestamp(configured, monkeypatch):
    store.write_key("KELLY-LED", _doc())
    fake = _patch(monkeypatch, _FakeClient(data=[[{"updated_at": "2026-08-04T11:00:00Z"}]]))
    assert await cloud_sync.push("KELLY-LED", store.read_key("KELLY-LED")) is True
    assert store.read_key("KELLY-LED")["updated_at"] == "2026-08-04T11:00:00Z"
    data_call = next(c for c in fake.calls if "/rest/v1/" in c[1])
    assert data_call[2]["params"]["on_conflict"] == "client_id,model_key"
    assert data_call[2]["json"][0]["client_id"] == "acme"


@pytest.mark.asyncio
async def test_push_queues_to_the_outbox_when_offline(configured, monkeypatch):
    _patch(monkeypatch, _FakeClient(raises=httpx.ConnectError("no network")))
    assert await cloud_sync.push("KELLY-LED", _doc()) is False
    queued = list(store.outbox_dir().glob("*.json"))
    assert len(queued) == 1
    assert json.loads(queued[0].read_text())["width"][0]["if_changes"] == "W@M [MIRROR-1]"


@pytest.mark.asyncio
async def test_flush_outbox_sends_and_clears(configured, monkeypatch):
    (store.outbox_dir() / "KELLY-LED.json").write_text(
        json.dumps(_doc(family="KELLY-LED", variant="default")))
    _patch(monkeypatch, _FakeClient(data=[[{"updated_at": "2026-08-04T11:30:00Z"}]]))
    assert await cloud_sync.flush_outbox() == 1
    assert list(store.outbox_dir().glob("*.json")) == []


@pytest.mark.asyncio
async def test_push_is_a_no_op_when_sync_is_off():
    assert await cloud_sync.push("KELLY-LED", _doc()) is False
    assert list(store.outbox_dir().glob("*.json")) == []      # nothing queued when disabled


# ── /sync-status ──────────────────────────────────────────────────────────────

def test_status_reports_off_when_unconfigured():
    st = cloud_sync.status()
    assert st["enabled"] is False and "reason" in st


def test_status_never_leaks_credentials(configured):
    st = cloud_sync.status()
    blob = json.dumps(st)
    assert st["enabled"] is True
    assert "hunter2-but-longer" not in blob and "test-anon-key" not in blob and TOKEN not in blob


@pytest.mark.asyncio
async def test_check_reports_ok_on_a_successful_round_trip(configured, monkeypatch):
    _patch(monkeypatch, _FakeClient(data=[[{"model_key": "KELLY-LED", "updated_at": "x"}]]))
    res = await cloud_sync.check()
    assert res["ok"] is True and res["rows_visible"] == 1 and res["client_id"] == "acme"


@pytest.mark.asyncio
async def test_check_explains_a_403_as_an_rls_problem(configured, monkeypatch):
    request = httpx.Request("GET", "https://example.supabase.co")
    response = httpx.Response(403, json={"message": "permission denied"}, request=request)
    _patch(monkeypatch, _FakeClient(
        data=[httpx.HTTPStatusError("403", request=request, response=response)]))
    res = await cloud_sync.check()
    assert res["ok"] is False and res["http"] == 403 and "RLS" in res["hint"]


@pytest.mark.asyncio
async def test_check_explains_a_404_as_a_missing_migration(configured, monkeypatch):
    request = httpx.Request("GET", "https://example.supabase.co")
    response = httpx.Response(404, json={"message": "not found"}, request=request)
    _patch(monkeypatch, _FakeClient(
        data=[httpx.HTTPStatusError("404", request=request, response=response)]))
    res = await cloud_sync.check()
    assert res["ok"] is False and "migration" in res["hint"]


@pytest.mark.asyncio
async def test_check_explains_a_failed_sign_in(configured, monkeypatch):
    bad = httpx.Response(400, json={"error": "invalid_grant"},
                         request=httpx.Request("POST", "https://example.supabase.co"))
    _patch(monkeypatch, _FakeClient(auth=bad))
    res = await cloud_sync.check()
    assert res["ok"] is False and "sign-in failed" in res["hint"]
