"""Places, and the Roster that serves both them and the cast.

Locations arrived last, and deliberately as a *noun*: a place accrues
description over time the way an NPC does, so it is a roster entry, not a
timeline tag. Rests and weather went the other way (see test_calendar.py) --
they are moments, so they are tags. Getting that distinction right is what
kept the fold-in parser out of the rests work.

The tests here therefore have two jobs: prove the fifth section parses, and
prove that cast and locations really are one implementation rather than two
that have drifted.
"""

import pytest

import server


HEAD = "## Ongoing threads\n- Find the relic.\n"
CAST = "## Cast updates\n- Yarrow: a hedge witch.\n"
EVENTS = "## New events\n- They crossed the bridge.\n"
PLACES = "## Location updates\n- The Sunken Chapel: a flooded ruin under the moor.\n"


# ------------------------------------------------- the generalised splitter ---

def test_a_locations_section_is_split_out():
    fold = server._split_fold_in_response(f"{HEAD}\n{CAST}\n{EVENTS}\n{PLACES}")
    assert "Sunken Chapel" in fold.locations
    assert "Sunken Chapel" not in fold.memory
    assert "Sunken Chapel" not in fold.events


@pytest.mark.parametrize("order", [
    (CAST, EVENTS, PLACES), (CAST, PLACES, EVENTS), (PLACES, CAST, EVENTS),
    (EVENTS, PLACES, CAST), (PLACES, EVENTS, CAST), (EVENTS, CAST, PLACES),
])
def test_the_sections_may_arrive_in_any_order(order):
    """Position decides where a section ends, not the order the prompt asked
    for -- which is the point of the rewrite. The old pairwise logic only
    handled two sections swapping."""
    fold = server._split_fold_in_response(HEAD + "\n" + "\n".join(order))
    assert "Yarrow" in fold.cast and "Yarrow" not in fold.events
    assert "crossed the bridge" in fold.events and "crossed the bridge" not in fold.cast
    assert "Sunken Chapel" in fold.locations
    assert fold.memory.strip().startswith("## Ongoing threads")


def test_a_missing_locations_section_is_empty_not_an_error():
    """The common case: a chunk of table talk that named no new place. An
    empty section means "nothing to add", exactly as for the cast."""
    fold = server._split_fold_in_response(f"{HEAD}\n{CAST}\n{EVENTS}")
    assert fold.locations == ""


def test_the_divider_fallback_works_for_locations_too():
    fold = server._split_fold_in_response(
        f"{HEAD}\n--- location updates ---\n- Harrow's Ford: a shallow crossing.\n")
    assert "Harrow's Ford" in fold.locations


def test_an_unrecognised_heading_stays_in_memory():
    """Never guessed at. A section we cannot identify is left in the working
    memory rather than routed somewhere permanent on a hunch."""
    fold = server._split_fold_in_response(f"{HEAD}\n## Place notes\n- Somewhere.\n")
    assert fold.locations == ""
    assert "Place notes" in fold.memory


# ---------------------------------------------------------------- the roster ---

def test_places_are_upserted_from_bullets(make_table):
    table, rt = make_table()
    server.LOCATIONS.apply_updates(table, rt, PLACES, "2026-08-21")
    places = server.LOCATIONS.listing(table)
    assert [p["name"] for p in places] == ["The Sunken Chapel"]
    assert places[0]["id"] == "the-sunken-chapel"
    assert places[0]["first_seen"] == "2026-08-21"


def test_a_later_fold_in_updates_the_description(make_table):
    table, rt = make_table()
    server.LOCATIONS.apply_updates(table, rt, PLACES, "2026-08-21")
    server.LOCATIONS.apply_updates(
        table, rt,
        "- The Sunken Chapel: drained, and full of nesting crows.\n", "2026-08-22")
    places = server.LOCATIONS.listing(table)
    assert len(places) == 1
    assert "crows" in places[0]["current"]
    assert places[0]["first_seen"] == "2026-08-21", "first_seen is not re-dated"


def test_an_amendment_survives_every_later_fold_in(make_table):
    """The rule the whole class exists to keep. A human said the chapel is a
    barrow; no amount of later machine text may quietly take that back."""
    table, rt = make_table()
    server.LOCATIONS.apply_updates(table, rt, PLACES, "2026-08-21")
    server.LOCATIONS.amend(table, rt, "the-sunken-chapel",
                           "It is a barrow, not a chapel.", "Rowan")
    server.LOCATIONS.apply_updates(
        table, rt, "- The Sunken Chapel: a flooded ruin, clearly a chapel.\n", "2026-08-22")

    place = server.LOCATIONS.listing(table)[0]
    assert place["current"] == "It is a barrow, not a chapel."
    assert "clearly a chapel" in place["auto"], "the machine's text is kept, just outranked"


def test_amending_something_that_does_not_exist_returns_none(make_table):
    table, rt = make_table()
    assert server.LOCATIONS.amend(table, rt, "nowhere", "...", "Rowan") is None


def test_a_bullet_without_a_description_is_skipped(make_table):
    table, rt = make_table()
    server.LOCATIONS.apply_updates(table, rt, "- Just a name\n- : just a colon\n",
                                   "2026-08-21")
    assert server.LOCATIONS.listing(table) == []


def test_a_corrupt_file_reads_as_empty_rather_than_raising(make_table):
    table, _rt = make_table()
    table.dir.mkdir(parents=True, exist_ok=True)
    table.locations_file.write_text("{not json", encoding="utf-8")
    assert server.LOCATIONS.listing(table) == []


def test_the_reference_text_shows_the_amended_description(make_table):
    """What goes back into the next fold-in prompt is what the entry says
    *now*, so the model does not keep re-proposing a corrected fact."""
    table, rt = make_table()
    server.LOCATIONS.apply_updates(table, rt, PLACES, "2026-08-21")
    server.LOCATIONS.amend(table, rt, "the-sunken-chapel", "A barrow.", "Rowan")
    assert server.LOCATIONS.reference_text(table) == "- The Sunken Chapel: A barrow."


# ------------------------------------------------ cast and places are one ---

def test_the_two_rosters_write_to_different_files(make_table):
    table, rt = make_table()
    server.CAST.apply_updates(table, rt, "- Yarrow: a hedge witch.\n", "2026-08-21")
    server.LOCATIONS.apply_updates(table, rt, PLACES, "2026-08-21")
    assert [e["name"] for e in server.CAST.listing(table)] == ["Yarrow"]
    assert [e["name"] for e in server.LOCATIONS.listing(table)] == ["The Sunken Chapel"]
    assert table.cast_file.exists() and table.locations_file.exists()


def test_the_two_rosters_do_not_share_a_lock(make_table):
    """Separate locks for separate files, per tenancy.py's convention -- a
    fold-in writing places must not block an amendment to an NPC."""
    _table, rt = make_table()
    assert server.CAST.lock(rt) is not server.LOCATIONS.lock(rt)


# --------------------------------------------------------------- endpoints ---

def test_places_can_be_read_and_amended_through_the_api(live):
    server.LOCATIONS.apply_updates(live.table, live.rt, PLACES, "2026-08-21")

    r = live.client.get(live.url("/locations"))
    assert r.status_code == 200
    assert [p["name"] for p in r.get_json()["places"]] == ["The Sunken Chapel"]

    r = live.client.post(live.url("/locations/the-sunken-chapel/amend"),
                         json={"text": "A barrow."})
    assert r.status_code == 200
    assert r.get_json()["current"] == "A barrow."


def test_players_can_amend_a_place(live):
    """Same as the cast: correcting the record is everyone's job, not the
    DM's alone."""
    server.LOCATIONS.apply_updates(live.table, live.rt, PLACES, "2026-08-21")
    player = live.player_client("Rowan")
    r = player.post(live.url("/locations/the-sunken-chapel/amend"),
                    json={"text": "A barrow."})
    assert r.status_code == 200
    assert r.get_json()["amendments"][-1]["by"] == "Rowan"


def test_amending_an_unknown_place_is_a_404_naming_it(live):
    r = live.client.post(live.url("/locations/nowhere/amend"), json={"text": "..."})
    assert r.status_code == 404
    assert r.get_json()["error"] == "unknown place"


def test_amending_an_unknown_npc_still_says_npc(live):
    """The noun comes from the roster, so the shared handler cannot start
    telling people their NPC is an unknown place."""
    r = live.client.post(live.url("/cast/nobody/amend"), json={"text": "..."})
    assert r.status_code == 404
    assert r.get_json()["error"] == "unknown npc"


def test_an_empty_correction_is_refused(live):
    server.LOCATIONS.apply_updates(live.table, live.rt, PLACES, "2026-08-21")
    r = live.client.post(live.url("/locations/the-sunken-chapel/amend"),
                         json={"text": "   "})
    assert r.status_code == 400


def test_places_do_not_cross_between_tables(live):
    other_user = live.db.create_user("o@example.com", "another-long-password")
    other_id = live.db.create_table(other_user, "Blackmoor", "blackmoor")

    from tenancy import Table
    from tenancy import get_runtime
    other = Table(live.db.table(other_id), server.DATA_DIR, server.BASE)
    server.LOCATIONS.apply_updates(live.table, live.rt, PLACES, "2026-08-21")

    assert server.LOCATIONS.listing(other) == []
    assert get_runtime(other.id).locations_lock is not live.rt.locations_lock


def test_a_stranger_cannot_read_the_places(live):
    stranger = server.app.test_client()
    r = stranger.get(live.url("/locations"))
    assert r.status_code in (401, 403)


# ------------------------------------------------------ routing a fold-in ---
#
# The tests above call the roster directly. These go through _summarize_ambient,
# which is where a section actually gets routed -- and where a place named in a
# chunk that introduced no NPC was briefly being dropped on the floor.

@pytest.fixture
def folds(monkeypatch):
    """Make the fold-in call return a canned response, so the routing either
    side of it can be tested without an LLM."""
    def _set(response):
        monkeypatch.setattr(server, "ask_llm", lambda *a, **k: response)
    return _set


def test_a_place_is_recorded_even_when_no_npc_was(make_table, folds):
    """Chunks that name a place but introduce nobody are ordinary -- a party
    arriving somewhere empty. The place must still land."""
    table, rt = make_table()
    folds(f"{HEAD}\n{PLACES}\n{EVENTS}")
    server._summarize_ambient(table, rt, "they reached the chapel")
    assert [p["name"] for p in server.LOCATIONS.listing(table)] == ["The Sunken Chapel"]


def test_a_fold_in_routes_cast_places_and_events_at_once(make_table, folds):
    table, rt = make_table()
    folds(f"{HEAD}\n{CAST}\n{PLACES}\n{EVENTS}")
    server._summarize_ambient(table, rt, "a busy scene")
    assert [n["name"] for n in server.CAST.listing(table)] == ["Yarrow"]
    assert [p["name"] for p in server.LOCATIONS.listing(table)] == ["The Sunken Chapel"]
    assert "crossed the bridge" in (table.sessions_dir / f"{_today()}-timeline.log").read_text()


def test_a_fold_in_naming_no_place_leaves_the_list_alone(make_table, folds):
    table, rt = make_table()
    server.LOCATIONS.apply_updates(table, rt, PLACES, "2026-08-21")
    folds(f"{HEAD}\n{CAST}")
    server._summarize_ambient(table, rt, "nobody went anywhere")
    assert [p["name"] for p in server.LOCATIONS.listing(table)] == ["The Sunken Chapel"]


def _today():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
