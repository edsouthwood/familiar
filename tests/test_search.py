"""Full-text search across everything a table has recorded.

Two things matter more than the matching itself.

The first is the tenancy boundary. `search` is the only table in the schema
where every tenant's prose sits together, so the table_id filter is not an
optimisation -- it is the wall, and it lives in SQL where a route cannot
forget it.

The second is that the index is derived. Every row in it copies something
already written to a file under the table's own directory, which is why the
live writes are best-effort and why tools/reindex_search.py can always put it
back. A test that pins that asymmetry is worth more than one that pins a
ranking.
"""

import json

import pytest

import db as database
import server


# ------------------------------------------------------- the query sanitiser ---
#
# FTS5's MATCH takes a real query language. A search box does not, and the
# gap between them is where a stray quote becomes a 500.

@pytest.mark.parametrize("raw", [
    '"', "'", "AND", "OR", "NOT", "NEAR(a b)", "a AND", "(", ")", "*", "-",
    "^foo", "x:y", "chapel*", '"unclosed', "a OR b NOT c", "  ", "", "😀",
    "col: value", "\\", "a\"b\"c",
])
def test_no_input_can_be_a_syntax_error(live, raw):
    """The whole point of quoting every token. Any of these fed raw to MATCH
    raises OperationalError; none of them may reach it."""
    server.db.index_doc(live.table.id, "event", "a/b", "The party crossed.", "2026-08-21")
    assert live.db.search(live.table.id, raw) is not None


def test_a_query_is_matched_as_literal_words():
    assert database.fts_query("sunken chapel") == '"sunken" "chapel"*'


def test_operators_are_defused_into_words():
    assert database.fts_query("a AND b") == '"a" "AND" "b"*'


def test_quotes_are_stripped_rather_than_escaped():
    """A quote inside a token would close the phrase we just opened. Dropping
    it is safe and predictable; escaping it is a second syntax to get wrong."""
    assert database.fts_query('say "hi"') == '"say" "hi"*'


def test_an_empty_query_produces_nothing_to_match():
    assert database.fts_query("") == ""
    assert database.fts_query("   ") == ""
    assert database.fts_query('"') == ""


def test_the_last_word_is_a_prefix_so_typing_finds_things(live):
    server.db.index_doc(live.table.id, "event", "a/b", "the Sunken Chapel", "2026-08-21")
    assert len(live.db.search(live.table.id, "chap")) == 1
    assert len(live.db.search(live.table.id, "chapel")) == 1


def test_an_earlier_word_is_not_a_prefix(live):
    """Only the word being typed is loose. "chap door" must not match
    "chapel doorway" on both halves, or every query would match everything."""
    server.db.index_doc(live.table.id, "event", "a/b", "the chapel doorway", "2026-08-21")
    assert live.db.search(live.table.id, "chap doorway") == []


# ------------------------------------------------------------------ upserts ---

def test_reindexing_the_same_ref_replaces_it(live):
    """An NPC's description is rewritten by every fold-in. Twenty fold-ins
    must leave one row, not twenty."""
    for text in ("a hedge witch", "a hedge witch who moved away", "a hedge witch, retired"):
        server.db.index_doc(live.table.id, "npc", "yarrow", f"Yarrow\n{text}")
    assert len(live.db.search(live.table.id, "Yarrow")) == 1
    assert live.db.search(live.table.id, "retired")
    assert live.db.search(live.table.id, "moved") == []


def test_indexing_empty_text_removes_the_document(live):
    server.db.index_doc(live.table.id, "npc", "yarrow", "Yarrow\na hedge witch")
    server.db.index_doc(live.table.id, "npc", "yarrow", "   ")
    assert live.db.search(live.table.id, "Yarrow") == []


# ------------------------------------------------------------------ tenancy ---

def test_one_tables_records_never_appear_in_anothers(live):
    other_user = live.db.create_user("o@example.com", "another-long-password")
    other_id = live.db.create_table(other_user, "Blackmoor", "blackmoor")
    server.db.index_doc(live.table.id, "event", "a/b", "the Sunken Chapel")
    server.db.index_doc(other_id, "event", "a/b", "the Sunken Chapel")

    assert len(live.db.search(live.table.id, "chapel")) == 1
    assert len(live.db.search(other_id, "chapel")) == 1
    assert live.db.search_counts(live.table.id, "chapel") == {"event": 1}


def test_clearing_one_tables_index_leaves_the_other(live):
    other_user = live.db.create_user("o@example.com", "another-long-password")
    other_id = live.db.create_table(other_user, "Blackmoor", "blackmoor")
    server.db.index_doc(live.table.id, "event", "a/b", "the Sunken Chapel")
    server.db.index_doc(other_id, "event", "a/b", "the Sunken Chapel")

    live.db.clear_index(live.table.id)
    assert live.db.search(live.table.id, "chapel") == []
    assert len(live.db.search(other_id, "chapel")) == 1


def test_a_stranger_cannot_search(live):
    stranger = server.app.test_client()
    assert stranger.get(live.url("/search?q=chapel")).status_code in (401, 403)


# ------------------------------------------------ what gets indexed, and when ---

def _at(day):
    from datetime import datetime, timezone
    return datetime(*(int(x) for x in day.split("-")), 19, 0, 0, tzinfo=timezone.utc)


def test_a_captured_event_becomes_searchable(live):
    server._append_timeline(live.table, live.rt,
                            "- They crossed the Sunken Chapel's flooded nave.",
                            when=_at("2026-08-21"))
    hits = live.db.search(live.table.id, "nave")
    assert [h["kind"] for h in hits] == ["event"]
    assert hits[0]["ref"] == "2026-08-21/2026-08-21-0000"
    assert hits[0]["date"] == "2026-08-21"


def test_a_correction_is_searchable_on_the_same_event(live):
    """One document per event, not one per amendment: searching a word that
    only a player's correction contains still finds the moment, once."""
    server._append_timeline(live.table, live.rt, "- They crossed the bridge.",
                            when=_at("2026-08-21"))
    server._append_amendment(live.table, live.rt, "2026-08-21", 0,
                             "They crossed the causeway.", "Rowan", {})
    hits = live.db.search(live.table.id, "causeway")
    assert len(hits) == 1
    assert hits[0]["kind"] == "event"
    # The original capture stays findable too -- it is a permanent record.
    assert len(live.db.search(live.table.id, "bridge")) == 1


def test_a_cast_entry_is_searchable_by_name_and_by_description(live):
    server.CAST.apply_updates(live.table, live.rt, "- Yarrow: a hedge witch.\n", "2026-08-21")
    assert [h["kind"] for h in live.db.search(live.table.id, "Yarrow")] == ["npc"]
    assert [h["kind"] for h in live.db.search(live.table.id, "hedge")] == ["npc"]
    assert live.db.search(live.table.id, "Yarrow")[0]["title"] == "Yarrow"


def test_a_place_is_indexed_as_a_place_not_an_npc(live):
    server.LOCATIONS.apply_updates(
        live.table, live.rt, "- The Sunken Chapel: a flooded ruin.\n", "2026-08-21")
    assert [h["kind"] for h in live.db.search(live.table.id, "flooded")] == ["place"]


def test_amending_a_roster_entry_updates_the_index(live):
    server.LOCATIONS.apply_updates(
        live.table, live.rt, "- The Sunken Chapel: a flooded ruin.\n", "2026-08-21")
    server.LOCATIONS.amend(live.table, live.rt, "the-sunken-chapel",
                           "A dry barrow, in fact.", "Rowan")
    assert live.db.search(live.table.id, "barrow")
    assert live.db.search(live.table.id, "flooded") == [], "the superseded text goes"


def test_a_question_and_its_answer_are_indexed_together(live):
    server._log_qa(live.table, "Whiskers, where are we?", "In the Sunken Chapel.")
    hits = live.db.search(live.table.id, "chapel")
    assert [h["kind"] for h in hits] == ["qa"]
    assert hits[0]["title"] == "Whiskers, where are we?"
    assert live.db.search(live.table.id, "where are we"), "the question matches too"


# ----------------------------------------------------------------- the route ---

def test_the_endpoint_returns_ranked_results_and_counts(live):
    server._append_timeline(live.table, live.rt, "- A stalactite fell.",
                            when=_at("2026-08-21"))
    server.CAST.apply_updates(live.table, live.rt,
                              "- Obo: a kobold who fears stalactites.\n", "2026-08-21")

    data = live.client.get(live.url("/search?q=stalactite")).get_json()
    assert data["counts"] == {"event": 1, "npc": 1}
    assert {r["kind"] for r in data["results"]} == {"event", "npc"}
    assert data["marks"] == [database.MARK_START, database.MARK_END]


def test_the_snippet_marks_the_match(live):
    server._append_timeline(live.table, live.rt, "- A stalactite fell on Bramble.",
                            when=_at("2026-08-21"))
    snippet = live.client.get(live.url("/search?q=stalactite")).get_json()["results"][0]["snippet"]
    assert database.MARK_START + "stalactite" + database.MARK_END in snippet


def test_the_snippet_never_contains_markup(live):
    """The client builds <mark> elements itself. Nothing transcribed from a
    room goes near innerHTML, so the server must not send tags."""
    server._append_timeline(live.table, live.rt, "- A stalactite fell.",
                            when=_at("2026-08-21"))
    snippet = live.client.get(live.url("/search?q=stalactite")).get_json()["results"][0]["snippet"]
    assert "<" not in snippet and ">" not in snippet


def test_the_kind_filter_narrows_the_results(live):
    server._append_timeline(live.table, live.rt, "- A stalactite fell.",
                            when=_at("2026-08-21"))
    server.CAST.apply_updates(live.table, live.rt,
                              "- Obo: a kobold who fears stalactites.\n", "2026-08-21")

    data = live.client.get(live.url("/search?q=stalactite&kind=npc")).get_json()
    assert [r["kind"] for r in data["results"]] == ["npc"]
    assert data["counts"] == {"event": 1, "npc": 1}, "counts describe the whole search"


def test_an_unknown_kind_is_refused(live):
    r = live.client.get(live.url("/search?q=x&kind=spells"))
    assert r.status_code == 400


def test_an_empty_query_is_not_an_error(live):
    r = live.client.get(live.url("/search?q="))
    assert r.status_code == 200
    assert r.get_json()["results"] == []


def test_a_nonsense_limit_is_refused(live):
    assert live.client.get(live.url("/search?q=x&limit=lots")).status_code == 400


def test_a_player_can_search(live):
    server._append_timeline(live.table, live.rt, "- A stalactite fell.",
                            when=_at("2026-08-21"))
    player = live.player_client("Rowan")
    assert player.get(live.url("/search?q=stalactite")).status_code == 200


# ------------------------------------------------------------ derived, not held ---

def test_the_index_can_be_rebuilt_from_the_files_alone(live):
    """The property the whole design leans on. Wipe the index, rebuild from
    what is on disk, and get the same answers back."""
    import tools.reindex_search as reindex

    server._append_timeline(live.table, live.rt, "- A stalactite fell on Bramble.",
                            when=_at("2026-08-21"))
    server.CAST.apply_updates(live.table, live.rt, "- Obo: a kobold.\n", "2026-08-21")
    server.LOCATIONS.apply_updates(live.table, live.rt,
                                   "- The Deep Mine: a warren of carts.\n", "2026-08-21")
    server._log_qa(live.table, "Where are we?", "In the Deep Mine.")
    before = {(h["kind"], h["ref"]) for h in live.db.search(live.table.id, "the")}

    live.db.clear_index(live.table.id)
    assert live.db.search(live.table.id, "stalactite") == []

    counts = reindex.index_table(live.table)
    assert counts == {"event": 1, "npc": 1, "place": 1, "qa": 1}
    assert {(h["kind"], h["ref"]) for h in live.db.search(live.table.id, "the")} == before


def test_the_rebuild_does_not_read_timeline_logs_as_questions(live):
    """sessions/ holds <date>.log, <date>-timeline.log and
    <date>-amendments.log side by side. Only the first is Q&A."""
    import tools.reindex_search as reindex

    server._append_timeline(live.table, live.rt, "- A stalactite fell.",
                            when=_at("2026-08-21"))
    server._append_amendment(live.table, live.rt, "2026-08-21", 0,
                             "A stalagmite, actually.", "Rowan", {})
    live.db.clear_index(live.table.id)
    assert reindex.index_table(live.table)["qa"] == 0


def test_a_dry_run_changes_nothing(live):
    import tools.reindex_search as reindex

    server._append_timeline(live.table, live.rt, "- A stalactite fell.",
                            when=_at("2026-08-21"))
    live.db.clear_index(live.table.id)
    assert reindex.index_table(live.table, dry_run=True)["event"] == 1
    assert live.db.search(live.table.id, "stalactite") == []


def test_indexing_failures_never_break_the_write(live, monkeypatch):
    """Bookkeeping must never be the reason an event is lost. The timeline
    file is the record; the index is a convenience over it."""
    def boom(*a, **k):
        raise RuntimeError("index is on fire")
    monkeypatch.setattr(server.db, "index_doc", boom)

    server._append_timeline(live.table, live.rt, "- A stalactite fell.",
                            when=_at("2026-08-21"))
    events = server._resolve_timeline_events(live.table, "2026-08-21")
    assert [e["current"] for e in events] == ["A stalactite fell."]
