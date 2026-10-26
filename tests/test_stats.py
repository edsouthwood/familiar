"""Dice tallies, joined to the party at read time.

Two things are being tested at once here, and they pull in opposite directions.

The tallies must be **forgiving**: a roll tag carries whatever name the model
heard, the party roster is often filled in weeks later, and a roll thrown at a
real table happened whether or not anybody has typed the character in yet. So
nothing is validated on the way in and nothing is ever discarded.

The presentation must be **exact**: "Seraphine" and "Seraphine the Paladin" are one
person and must not appear as two, but they can only be joined by a human
saying so. So the join is exact, the leftovers get their own block instead of
being merged by guesswork, and the fix is an alias.

Before this, `/api/stats` had no tests at all and returned a dictionary keyed
on whatever string the fold-in emitted -- so a character who had never rolled
did not exist, and three spellings of one name were three characters.
"""

from datetime import datetime, timezone

import pytest

import server


def _at(day: str, hour: int = 19) -> datetime:
    return datetime(*(int(x) for x in day.split("-")), hour, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def rolls(live):
    """A table with a party and some dice already thrown."""
    def _roll(character, outcome, day="2026-08-21"):
        server._append_timeline(
            live.table, live.rt,
            f"- {character} rolled. {{roll: character={character}, outcome={outcome}}}",
            when=_at(day))
    live.roll = _roll
    return live


def _stats(live):
    return live.client.get(live.url("/stats")).get_json()


# --------------------------------------------------------------- the tallies ---

def test_a_roll_lands_on_its_character(rolls):
    rolls.client.post(rolls.url("/party"), json={"name": "Seraphine"})
    rolls.roll("Seraphine", "success")
    rolls.roll("Seraphine", "nat1")

    body = _stats(rolls)
    seraphine = next(c for c in body["characters"] if c["id"] == "seraphine")
    assert seraphine["tally"] == {"success": 1, "failure": 0, "crit": 0, "nat1": 1}
    assert body["total_events_with_rolls"] == 2


def test_a_character_who_has_never_rolled_still_appears(rolls):
    """The thing the old shape could not express. Corwin has been at every
    session and thrown nothing; a stats table that omits him is wrong about
    the party, not merely quiet about the dice."""
    rolls.client.post(rolls.url("/party"), json={"name": "Corwin"})
    body = _stats(rolls)
    corwin = next(c for c in body["characters"] if c["id"] == "corwin")
    assert corwin["tally"] == {"success": 0, "failure": 0, "crit": 0, "nat1": 0}


def test_an_empty_table_has_no_characters_and_no_error(rolls):
    body = _stats(rolls)
    assert body["characters"] == [] and body["unplaced"] == []


# ------------------------------------------------------------- the join ---

def test_case_and_spacing_differences_are_one_character(rolls):
    """The common mis-transcription, and the one that is safe to merge because
    it cannot possibly be two people."""
    rolls.client.post(rolls.url("/party"), json={"name": "Seraphine"})
    rolls.roll("Seraphine", "success")
    rolls.roll("seraphine", "failure")
    rolls.roll("  SERAPHINE  ", "crit")

    body = _stats(rolls)
    assert len(body["characters"]) == 1
    assert body["characters"][0]["tally"] == {"success": 1, "failure": 1, "crit": 1, "nat1": 0}
    assert body["unplaced"] == []


def test_a_name_that_matches_nobody_gets_its_own_block(rolls):
    """Not a fifth character. "Seraphine the Paladin" in the table beside "Seraphine"
    reads as two people and is really one bad transcription -- and merging them
    without being told to would be a guess about who somebody is."""
    rolls.client.post(rolls.url("/party"), json={"name": "Seraphine"})
    rolls.roll("Seraphine", "success")
    rolls.roll("Seraphine the Paladin", "failure")

    body = _stats(rolls)
    assert [c["id"] for c in body["characters"]] == ["seraphine"]
    assert body["characters"][0]["tally"]["success"] == 1
    assert [u["name"] for u in body["unplaced"]] == ["Seraphine the Paladin"]
    assert body["unplaced"][0]["tally"]["failure"] == 1


def test_teaching_it_an_alias_moves_the_rolls_across(rolls):
    """The whole day-one workflow: the first render shows unplaced names, the
    DM says "this is Seraphine", and the numbers move. No re-recording, because
    the join is at read time."""
    rolls.client.post(rolls.url("/party"), json={"name": "Seraphine"})
    rolls.roll("Seraphine the Paladin", "failure")
    assert _stats(rolls)["unplaced"]

    rolls.client.put(rolls.url("/party/seraphine"), json={"aliases": ["Seraphine the Paladin"]})

    body = _stats(rolls)
    assert body["unplaced"] == []
    assert body["characters"][0]["tally"]["failure"] == 1


def test_rolls_survive_the_party_being_written_afterwards(rolls):
    """The roster is often filled in weeks after the session that produced the
    rolls. Validating names on the way in would have thrown those away."""
    rolls.roll("Seraphine", "success")
    assert [u["name"] for u in _stats(rolls)["unplaced"]] == ["Seraphine"]

    rolls.client.post(rolls.url("/party"), json={"name": "Seraphine"})
    assert _stats(rolls)["unplaced"] == []
    assert _stats(rolls)["characters"][0]["tally"]["success"] == 1


# --------------------------------------------------------- retired and gone ---

def test_a_retired_character_keeps_their_dice_history(rolls):
    """Retiring is what happens when a character dies or leaves. Their rolls
    happened, so they stay -- flagged, not deleted."""
    rolls.client.post(rolls.url("/party"), json={"name": "Seraphine"})
    rolls.roll("Seraphine", "crit")
    rolls.client.put(rolls.url("/party/seraphine"), json={"retired": True})

    seraphine = next(c for c in _stats(rolls)["characters"] if c["id"] == "seraphine")
    assert seraphine["retired"] is True
    assert seraphine["tally"]["crit"] == 1


def test_a_deleted_characters_rolls_are_not_lost(rolls):
    """Deleting the roster entry does not un-throw the dice. They come back as
    an unplaced name, which is honest: something rolled, and we no longer know
    who."""
    rolls.client.post(rolls.url("/party"), json={"name": "Seraphine"})
    rolls.roll("Seraphine", "success")
    rolls.client.delete(rolls.url("/party/seraphine"), json={})

    body = _stats(rolls)
    assert body["total_events_with_rolls"] == 1
    assert [u["name"] for u in body["unplaced"]] == ["Seraphine"]


# ------------------------------------------------------------- amendments ---

def test_amending_a_roll_moves_the_tally(rolls):
    """The property api_stats has always had and which must survive the
    reshape: the numbers are derived, so a correction needs no separate
    invalidation."""
    rolls.client.post(rolls.url("/party"), json={"name": "Seraphine"})
    rolls.roll("Seraphine", "success")

    rolls.client.post(rolls.url("/sessions/2026-08-21/timeline/2026-08-21-0000/amend"),
                      json={"text": "Seraphine rolled.",
                            "roll": {"character": "Seraphine", "outcome": "nat1"}})

    tally = _stats(rolls)["characters"][0]["tally"]
    assert tally == {"success": 0, "failure": 0, "crit": 0, "nat1": 1}


def test_clearing_a_roll_removes_it_from_the_tally(rolls):
    rolls.client.post(rolls.url("/party"), json={"name": "Seraphine"})
    rolls.roll("Seraphine", "success")
    rolls.client.post(rolls.url("/sessions/2026-08-21/timeline/2026-08-21-0000/amend"),
                      json={"text": "Seraphine did something else.", "roll": None})

    body = _stats(rolls)
    assert body["total_events_with_rolls"] == 0
    assert body["characters"][0]["tally"]["success"] == 0


# --------------------------------------------------------------- the basics ---

def test_rolls_are_counted_across_dates(rolls):
    rolls.client.post(rolls.url("/party"), json={"name": "Seraphine"})
    rolls.roll("Seraphine", "success", "2026-08-20")
    rolls.roll("Seraphine", "success", "2026-08-21")
    assert _stats(rolls)["characters"][0]["tally"]["success"] == 2


def test_a_player_can_read_the_stats(rolls):
    player = rolls.player_client("Rowan")
    assert player.get(rolls.url("/stats")).status_code == 200


def test_a_stranger_cannot(rolls):
    stranger = server.app.test_client()
    assert stranger.get(rolls.url("/stats")).status_code in (401, 403)


def test_stats_do_not_cross_between_tables(rolls):
    other_user = rolls.db.create_user("o@example.com", "another-long-password")
    other_id = rolls.db.create_table(other_user, "Blackmoor", "blackmoor")
    rolls.client.post(rolls.url("/party"), json={"name": "Seraphine"})
    rolls.roll("Seraphine", "success")

    other = server.app.test_client()
    other.post("/api/login", json={"email": "o@example.com",
                                   "password": "another-long-password"})
    body = other.get(f"/api/t/blackmoor/stats").get_json()
    assert body["characters"] == [] and body["total_events_with_rolls"] == 0
