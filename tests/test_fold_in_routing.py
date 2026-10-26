"""End-to-end routing of one fold-in response to its four destinations.

The unit tests pin the parsing; this pins the wiring -- that bond lands in the
*familiar's* directory while memory, cast and timeline stay with the table. That
split is the reason familiars can be swapped mid-campaign, so it is worth a test
that actually touches the files.
"""

import json

import pytest

import server
from familiars import Familiars


FOLD_IN_RESPONSE = """## Party & relationships
Pip is adored by Seraphine and merely tolerated by Tam.

## Ongoing threads
- Find the amulet before the equinox.

## Recent developments
- They reached the bridge.

## Cast updates
- Yarrow: a nervous hedge witch who trades in river-glass.

## New events
- Tam picked the lock on the shrine door. {roll: character=Tam, outcome=crit}
- The party crossed the bridge.
"""


@pytest.fixture
def table(make_table, monkeypatch):
    """A whole isolated table: its own data dir, familiars and a stub LLM.

    Nothing is monkeypatched onto server's paths any more -- the Table carries
    them, so the fold-in physically cannot write outside tmp_path.
    """
    t, rt = make_table("pip-table")
    (t.app_root / "persona.example.md").write_text("A default familiar.", encoding="utf-8")

    fams = t.familiars
    fams.create("Pip", persona="You are Pip.")
    fams.set_active("pip")

    calls = []

    def fake_ask_llm(system, prompt, history, **kwargs):
        # **kwargs absorbs model= and on_usage=, which the metering pass added.
        # Recorded so the tests below can inspect each call.
        calls.append({"system": system, "prompt": prompt, **kwargs})
        return FOLD_IN_RESPONSE

    monkeypatch.setattr(server, "ask_llm", fake_ask_llm)

    class Harness:
        data = t.dir
        table = t
        runtime = rt
        # `familiars` is rebuilt from the directory each time it is asked for,
        # so read it fresh rather than holding the instance the fixture made.
        @property
        def fams(self):
            return t.familiars

        def fold(self, text):
            server._summarize_ambient(t, rt, text)

    harness = Harness()
    harness.calls = calls
    return harness


def test_fold_in_routes_each_section_to_its_own_home(table):
    table.fold("...transcript...")

    memory = (table.data / "game-context.md").read_text(encoding="utf-8")
    assert "Find the amulet" in memory
    assert "reached the bridge" in memory
    assert "adored by Seraphine" not in memory, "bond must not stay in table-level memory"
    assert "Yarrow" not in memory and "picked the lock" not in memory

    bond = table.fams.bond_text(table.fams.active())
    assert "adored by Seraphine" in bond
    assert "Find the amulet" not in bond

    cast = json.loads((table.data / "cast.json").read_text(encoding="utf-8"))
    assert any(n["name"] == "Yarrow" for n in cast["npcs"].values())

    (timeline,) = (table.data / "sessions").glob("*-timeline.log")
    lines = [json.loads(l) for l in timeline.read_text(encoding="utf-8").splitlines()]
    assert [l["event"] for l in lines] == [
        "Tam picked the lock on the shrine door.",
        "The party crossed the bridge.",
    ]
    assert lines[0]["roll"] == {"character": "Tam", "outcome": "crit"}


def test_fold_in_prompt_uses_the_active_familiars_name(table):
    table.fold("...transcript...")
    (call,) = table.calls
    assert "Pip" in call["system"]
    assert "Whiskers" not in call["system"] and "Whiskers" not in call["prompt"]
    assert "Pip's running session memory" in call["prompt"]


def test_fold_in_shows_the_model_the_existing_bond(table):
    """bond and memory live in two files but reach the model as one document,
    so it can carry the relationship forward instead of reinventing it."""
    table.fold("first chunk")
    table.calls.clear()
    table.fold("second chunk")

    (call,) = table.calls
    assert "adored by Seraphine" in call["prompt"], "prior bond must be visible to the fold-in"
    assert "Find the amulet" in call["prompt"], "prior memory must be visible too"


def test_bond_belongs_to_the_familiar_not_the_table(table):
    """Swapping familiars leaves the campaign intact and the new familiar
    without an inherited relationship."""
    table.fold("...transcript...")
    table.fams.create("Grix", persona="You are Grix.")
    table.fams.set_active("grix")

    assert table.fams.bond_text(table.fams.get("pip")) != ""
    assert table.fams.bond_text(table.fams.get("grix")) == ""
    assert "Find the amulet" in (table.data / "game-context.md").read_text(encoding="utf-8")


def test_degenerate_empty_response_preserves_existing_memory(table):
    table.fold("...transcript...")
    before = (table.data / "game-context.md").read_text(encoding="utf-8")

    server.ask_llm = lambda *a, **k: "   "
    table.fold("...more...")

    assert (table.data / "game-context.md").read_text(encoding="utf-8") == before
    assert "adored by Seraphine" in table.fams.bond_text(table.fams.active())


def test_response_without_a_bond_section_leaves_the_stored_bond_alone(table):
    table.fold("...transcript...")
    server.ask_llm = lambda *a, **k: "## Ongoing threads\n- Something else entirely.\n"
    table.fold("...more...")

    assert "adored by Seraphine" in table.fams.bond_text(table.fams.active())
    assert "Something else entirely" in (table.data / "game-context.md").read_text(encoding="utf-8")
