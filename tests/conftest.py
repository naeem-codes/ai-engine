import os

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point the rules store at a temp dir for EVERY test.

    Without this the store would resolve %PROGRAMDATA%\\LumiDesignAI\\rules — the real one on
    the dev machine — so a locally saved rule set could satisfy a test that is supposed to
    find nothing, and a test could overwrite the developer's own rules.
    """
    monkeypatch.setenv("LUMI_DATA_DIR", str(tmp_path / "data"))
    # Sync must never be attempted from the suite, whatever is in the developer's .env.
    monkeypatch.setenv("RULES_REMOTE", "none")
    for var in ("SUPABASE_URL", "SUPABASE_KEY", "CLIENT_ID"):
        monkeypatch.delenv(var, raising=False)
    yield
