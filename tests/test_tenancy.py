"""Two tables must never be able to reach each other.

This is the acceptance test for Phase 1. Everything else in the refactor was
mechanical -- threading a `table` argument through functions that used to read a
module constant. What actually matters is whether the threading is complete, and
the only honest way to check that is to run two tables side by side and look for
leaks.
"""

import json

import pytest

import server
from tenancy import Registry, Table, TableRuntime


@pytest.fixture
def two_tables(live):
    """A second table, owned by a second DM, beside the fixture's own."""
    other_user = live.db.create_user("other@example.com", "another-long-password")
    other_id = live.db.create_table(other_user, "Blackmoor", "blackmoor")
    other = Table(live.db.table(other_id), server.DATA_DIR, server.BASE)
    other.ensure_dirs()

    client = server.app.test_client()
    client.post("/api/login",
                json={"email": "other@example.com", "password": "another-long-password"})

    class Other:
        table = other
        row = live.db.table(other_id)

        def url(self, path):
            return f"/api/t/blackmoor{path}"

    o = Other()
    o.client = client
    return live, o


# ----------------------------------------------------------------- files ----

def test_each_table_gets_its_own_directory(two_tables):
    mine, theirs = two_tables
    assert mine.table.dir != theirs.table.dir
    assert mine.table.dir.name == mine.row["id"]
    assert theirs.table.dir.name == theirs.row["id"]


def test_the_directory_is_named_by_id_not_slug(live):
    """Slugs are renameable. If the directory were named after the slug, a
    rename would orphan every session log the table had ever written."""
    assert live.table.dir.name == live.row["id"]
    live.db.rename_table(live.row["id"], slug="renamed")
    again = Table(live.db.table(live.row["id"]), server.DATA_DIR, server.BASE)
    assert again.dir == live.table.dir


def test_a_fold_in_writes_only_into_its_own_table(two_tables, monkeypatch):
    mine, theirs = two_tables
    monkeypatch.setattr(server, "ask_llm", lambda *a, **k: (
        "## Ongoing threads\n- Mine alone.\n\n"
        "## Cast updates\n- Yarrow: a hedge witch.\n\n"
        "## New events\n- The party crossed the bridge.\n"
    ))
    server._summarize_ambient(mine.table, TableRuntime(mine.table.id), "chatter")

    assert "Mine alone" in mine.table.game_context_file.read_text(encoding="utf-8")
    assert not theirs.table.game_context_file.exists()
    assert not theirs.table.cast_file.exists()
    assert list(theirs.table.sessions_dir.glob("*")) == []


def test_the_cast_endpoints_do_not_cross(two_tables):
    mine, theirs = two_tables
    mine.table.cast_file.write_text(json.dumps(
        {"npcs": {"yarrow": {"name": "Yarrow", "auto": "a hedge witch"}}}), encoding="utf-8")

    assert [n["name"] for n in mine.client.get(mine.url("/cast")).get_json()["npcs"]] == ["Yarrow"]
    assert theirs.client.get(theirs.url("/cast")).get_json()["npcs"] == []


def test_familiars_belong_to_one_table(two_tables):
    mine, theirs = two_tables
    mine.table.familiars.create("Pip")
    theirs.table.familiars.create("Grix")

    assert [f.name for f in mine.table.familiars.list()] == ["Pip"]
    assert [f.name for f in theirs.table.familiars.list()] == ["Grix"]

    assert mine.client.get(mine.url("/config")).get_json()["name"] == "Pip"
    assert theirs.client.get(theirs.url("/config")).get_json()["name"] == "Grix"


def test_switching_a_familiar_does_not_touch_the_other_table(two_tables):
    mine, theirs = two_tables
    for t in (mine.table, theirs.table):
        t.familiars.create("Pip")
        t.familiars.create("Grix")
        t.familiars.set_active("pip")

    assert mine.client.post(mine.url("/familiar"), json={"id": "grix"}).status_code == 200

    assert mine.table.familiars.active().id == "grix"
    assert theirs.table.familiars.active().id == "pip", "the other table was dragged along"


# --------------------------------------------------------------- runtime ----

def test_history_is_per_table(two_tables, monkeypatch):
    mine, theirs = two_tables
    for t in (mine.table, theirs.table):
        t.familiars.create("Pip", persona="You are Pip.")
    monkeypatch.setattr(server, "ask_llm",
                        lambda system, q, history, **kw: f"turns={len(history)}")

    mine.client.post(mine.url("/ask"), json={"question": "who goes there?"})
    mine.client.post(mine.url("/ask"), json={"question": "and now?"})
    r = theirs.client.post(theirs.url("/ask"), json={"question": "first question here"})

    assert r.get_json()["answer"] == "turns=0", "the other table's history leaked in"


def test_forget_clears_only_this_tables_history(two_tables, monkeypatch):
    mine, theirs = two_tables
    for t in (mine.table, theirs.table):
        t.familiars.create("Pip", persona="You are Pip.")
    monkeypatch.setattr(server, "ask_llm",
                        lambda system, q, history, **kw: f"turns={len(history)}")

    mine.client.post(mine.url("/ask"), json={"question": "one"})
    theirs.client.post(theirs.url("/ask"), json={"question": "one"})
    mine.client.post(mine.url("/forget"), json={})

    assert mine.client.post(mine.url("/ask"), json={"q": "x", "question": "two"}
                            ).get_json()["answer"] == "turns=0"
    assert theirs.client.post(theirs.url("/ask"), json={"question": "two"}
                              ).get_json()["answer"] == "turns=1"


def test_the_registry_hands_back_the_same_runtime(live):
    registry = Registry()
    assert registry.get("a") is registry.get("a")
    assert registry.get("a") is not registry.get("b")


def test_locks_are_not_shared_between_tables():
    """One global lock would be correct and slow: every table's fold-in would
    queue behind every other table's."""
    a, b = TableRuntime("a"), TableRuntime("b")
    for name in ("ambient_lock", "cast_lock", "timeline_lock", "game_context_lock"):
        assert getattr(a, name) is not getattr(b, name), name


def test_broadcast_reaches_only_this_tables_bodies():
    a, b = TableRuntime("a"), TableRuntime("b")

    class Body:
        def __init__(self):
            self.got = []

        def send(self, data):
            self.got.append(data)

    mine, source, elsewhere = Body(), Body(), Body()
    a.add_body(mine)
    a.add_body(source)
    b.add_body(elsewhere)

    a.broadcast('{"type":"state","state":"thinking"}', source=source)

    assert len(mine.got) == 1
    assert source.got == [], "a body must not receive its own broadcast"
    assert elsewhere.got == [], "another table's body must never hear this"
