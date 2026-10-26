"""Shared fixtures.

The interesting one is `make_table`. Before Phase 1, a test that touched the
filesystem had to monkeypatch four module globals in server.py
(SESSIONS_DIR, GAME_CONTEXT_FILE, CAST_FILE, familiars) and hope it had found
them all -- a test could pass while quietly writing into the real repo. Now
every path hangs off a Table, so a test builds one over tmp_path and the code
under test cannot reach anywhere else. That is the refactor paying for itself.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tenancy import Table, TableRuntime  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Every test client arrives from 127.0.0.1, so without this the suite's
    own logins would add up to a lockout somewhere around the fortieth test."""
    import ratelimit
    ratelimit.reset_all()
    yield
    ratelimit.reset_all()


@pytest.fixture
def make_table(tmp_path):
    """Build an isolated table on disk. Returns (Table, TableRuntime).

    The row is a plain dict rather than a sqlite Row: Table only ever indexes
    it, and a test about where files land should not need a database.
    """
    def _make(slug="test", app_root=None, plan="base"):
        root = tmp_path / "root"
        app = Path(app_root) if app_root else tmp_path / "app"
        app.mkdir(parents=True, exist_ok=True)
        # The tracked application defaults. They live in the app root, not the
        # table, and code falls back to them for a table that has not written
        # its own -- so a fixture without them tests a state that cannot exist.
        for name in ("persona", "campaign"):
            default = app / f"{name}.example.md"
            if not default.exists():
                default.write_text(f"A default {name}.\n", encoding="utf-8")
        row = {
            "id": slug, "slug": slug, "name": slug.title(),
            "owner_id": "owner", "plan": plan, "invite_token": f"tok-{slug}",
        }
        table = Table(row, root, app)
        table.ensure_dirs()
        return table, TableRuntime(table.id)
    return _make


@pytest.fixture
def table_pair(make_table):
    """Two tables side by side, for the isolation tests -- the ones that prove
    one table's fold-in, voice change or amendment cannot reach another's."""
    return make_table("alpha"), make_table("beta")


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A running app with a real database, one DM and one table.

    Returns an object with:
        .db      the Database
        .table   the Table (paths under tmp_path)
        .rt      that table's TableRuntime
        .client  a Flask test client already signed in as the DM
        .player_client(name)  a second client joined through the invite link
        .url(path)            "/api/t/<slug>{path}"

    Everything -- the database, the data root, the app root -- is redirected
    into tmp_path, so a test cannot touch the developer's real campaign.
    """
    import db as database
    import server

    monkeypatch.setenv("SECRET_KEY", "test-key-not-a-secret")
    data_root = tmp_path / "data"
    app_root = tmp_path / "app"
    app_root.mkdir(parents=True, exist_ok=True)
    for name in ("persona", "campaign"):
        (app_root / f"{name}.example.md").write_text(f"A default {name}.\n", encoding="utf-8")

    handle = database.Database(data_root / "app.db")
    monkeypatch.setattr(server, "db", handle)
    monkeypatch.setattr(server, "DATA_DIR", data_root)
    monkeypatch.setattr(server, "BASE", app_root)
    monkeypatch.setitem(server.app.config, "DB", handle)
    monkeypatch.setitem(server.app.config, "DATA_DIR", data_root)
    monkeypatch.setitem(server.app.config, "APP_ROOT", app_root)

    user_id = handle.create_user("dm@example.com", "a-long-enough-password")
    table_id = handle.create_table(user_id, "Grove", "grove")
    row = handle.table(table_id)
    table = Table(row, data_root, app_root)
    table.ensure_dirs()

    client = server.app.test_client()
    client.post("/api/login",
                json={"email": "dm@example.com", "password": "a-long-enough-password"})

    class Live:
        def __init__(self):
            self.db = handle
            self.table = table
            self.row = row
            self.client = client
            self.user_id = user_id

        @property
        def rt(self):
            """The table's runtime -- the same one the app's request handlers
            get, so a test can seed a file through the locks the server uses
            rather than around them."""
            return server.get_runtime(table.id)

        def url(self, path):
            return f"/api/t/{row['slug']}{path}"

        def url_page(self, page):
            """A table's HTML pages live under /t/<slug>/, not /api/t/<slug>/."""
            return f"/t/{row['slug']}/{page}"

        def player_client(self, name="Rowan"):
            """A second browser that joined by invitation -- no account, just a
            member cookie. The fixture's own client stays signed in as the DM,
            so the two can be used side by side in one test."""
            other = server.app.test_client()
            token = handle.table(table_id)["invite_token"]
            other.post(f"/api/join/{token}", json={"name": name})
            return other

    return Live()
