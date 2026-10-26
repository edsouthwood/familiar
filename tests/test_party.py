"""The party: who is at the table in the fiction.

Not a Roster, and the tests are where that distinction has to hold. A cast
member is written by the machine and corrected by a human; a party member is
written by a human and must never be touched by the machine at all. The two
look alike and want opposite defaults for nearly every question.

The property that carries the most weight is that **an id is assigned once**.
Roll tallies hang off it, so a rename that re-derived the id would silently
orphan every die that character has ever thrown -- which is exactly the "known
sharp edge" the places list already has, and is tolerable there and not here.
"""

import pytest

import server


PC = {"name": "Seraphine", "class": "Paladin", "pronouns": "she/her",
      "player": "Alex", "notes": "Princess of Westmarch."}


# ------------------------------------------------------------- the entries ---

def test_a_character_is_created_with_an_id_from_their_name(make_table):
    table, rt = make_table()
    entry, error = server.PARTY.create(table, rt, PC, by="Alex")
    assert error is None
    assert entry["id"] == "seraphine"
    assert entry["name"] == "Seraphine" and entry["class"] == "Paladin"
    assert entry["retired"] is False


def test_the_id_survives_a_rename(make_table):
    """The whole reason Party is not a Roster. A Roster re-slugs the name on
    every write, so renaming forks the entry -- fine for a cave, ruinous for a
    character whose dice history is keyed on it."""
    table, rt = make_table()
    created, _ = server.PARTY.create(table, rt, PC)
    renamed, _ = server.PARTY.update(table, rt, created["id"],
                                     {"name": "Queen Seraphine of Westmarch"})
    assert renamed["id"] == created["id"] == "seraphine"
    assert renamed["name"] == "Queen Seraphine of Westmarch"
    assert len(server.PARTY.listing(table)) == 1, "a rename must not fork the entry"


def test_two_characters_with_the_same_name_get_distinct_ids(make_table):
    """Two players called Bob is a real table."""
    table, rt = make_table()
    first, _ = server.PARTY.create(table, rt, {"name": "Bob"})
    second, _ = server.PARTY.create(table, rt, {"name": "Bob"})
    assert first["id"] != second["id"]
    assert len(server.PARTY.listing(table)) == 2


def test_a_name_that_slugifies_to_nothing_still_gets_an_id(make_table):
    table, rt = make_table()
    entry, error = server.PARTY.create(table, rt, {"name": "???"})
    assert error is None and entry["id"]


def test_a_character_needs_a_name(make_table):
    table, rt = make_table()
    assert server.PARTY.create(table, rt, {"name": "   "})[1]
    assert server.PARTY.create(table, rt, {"class": "Paladin"})[1]


def test_a_caller_may_not_choose_or_change_the_id(make_table):
    """The id is identity, not data. It is assigned from the name once and
    every roll that character makes is keyed on it, so a caller who could set
    it could point one character's dice history at another."""
    table, rt = make_table()
    assert server.PARTY.create(table, rt, dict(PC, id="somebody-else"))[1]
    server.PARTY.create(table, rt, PC)
    assert server.PARTY.update(table, rt, "seraphine", {"id": "somebody-else"})[1]
    assert server.PARTY.get(table, "seraphine")["id"] == "seraphine"


def test_an_unknown_field_is_refused_rather_than_stored(make_table):
    table, rt = make_table()
    assert "unknown field" in server.PARTY.create(table, rt, {"name": "X", "hp": 12})[1]


def test_a_corrupt_file_reads_as_empty(make_table):
    table, _rt = make_table()
    table.dir.mkdir(parents=True, exist_ok=True)
    table.party_file.write_text("{not json", encoding="utf-8")
    assert server.PARTY.listing(table) == []


def test_retired_characters_sort_last(make_table):
    table, rt = make_table()
    server.PARTY.create(table, rt, {"name": "Zara"})
    gone, _ = server.PARTY.create(table, rt, {"name": "Alfred"})
    server.PARTY.update(table, rt, gone["id"], {"retired": True})
    assert [e["name"] for e in server.PARTY.listing(table)] == ["Zara", "Alfred"]


# -------------------------------------------------------------- the aliases ---

def test_aliases_are_deduplicated_the_way_they_are_matched(make_table):
    """Two spellings of one alias both sitting there would look like they meant
    different things."""
    table, rt = make_table()
    entry, _ = server.PARTY.create(table, rt, {"name": "Seraphine",
                                               "aliases": ["El", "el", "  EL  "]})
    assert entry["aliases"] == ["El"]


def test_aliases_must_be_a_list_of_names(make_table):
    table, rt = make_table()
    assert server.PARTY.create(table, rt, {"name": "X", "aliases": "El"})[1]
    assert server.PARTY.create(table, rt, {"name": "X", "aliases": [1, 2]})[1]


# ------------------------------------------------------------- the matching ---

@pytest.fixture
def party(make_table):
    table, rt = make_table()
    server.PARTY.create(table, rt, dict(PC, aliases=["El", "the princess"]))
    server.PARTY.create(table, rt, {"name": "Bramble", "notes": "A unicorn."})
    return table, rt


def test_an_exact_name_matches(party):
    table, _rt = party
    assert server.PARTY.match(table, "Seraphine") == "seraphine"


def test_case_and_spacing_do_not_matter(party):
    table, _rt = party
    assert server.PARTY.match(table, "  sErApHiNe ") == "seraphine"
    assert server.PARTY.match(table, "the  PRINCESS") == "seraphine"


def test_an_alias_matches(party):
    table, _rt = party
    assert server.PARTY.match(table, "El") == "seraphine"


def test_a_near_miss_does_not_match(party):
    """The accepted imperfection, asserted on purpose. Exact matching is only
    liveable because a human can add an alias; a fuzzy matcher would merge two
    genuinely different characters silently, and this test is what stops
    somebody adding one later."""
    assert server.PARTY.match(party[0], "Seraphine the Paladin") is None
    assert server.PARTY.match(party[0], "Elar") is None
    assert server.PARTY.match(party[0], "") is None


def test_teaching_it_an_alias_is_the_fix(party):
    table, rt = party
    assert server.PARTY.match(table, "Seraphine the Paladin") is None
    server.PARTY.update(table, rt, "seraphine", {"aliases": ["El", "Seraphine the Paladin"]})
    assert server.PARTY.match(table, "Seraphine the Paladin") == "seraphine"


# --------------------------------------------------------------- the prompt ---

def test_the_prompt_text_lists_the_party(party):
    text = server.PARTY.prompt_text(party[0])
    assert "Seraphine" in text and "Paladin" in text and "Played by Alex" in text
    assert "also called El" in text


def test_a_retired_character_leaves_the_prompt(party):
    """They are not at the table any more, and listing them invites the fold-in
    to tag rolls for somebody who left."""
    table, rt = party
    server.PARTY.update(table, rt, "bramble", {"retired": True})
    text = server.PARTY.prompt_text(table)
    assert "Bramble" not in text and "Seraphine" in text


def test_the_familiar_is_told_who_is_at_the_table(make_table):
    """prompt_text() is only worth having if it reaches the model. Asserted on
    the stable half specifically: the party changes between sessions, not
    during one, so it belongs in the cacheable prefix."""
    table, rt = make_table()
    server.PARTY.create(table, rt, PC)
    stable = server.build_system_prompt(table).stable
    assert "Seraphine" in stable and "Paladin" in stable


def test_a_table_with_no_party_says_nothing_about_one(make_table):
    """A heading over an empty list invites the model to invent an answer to
    "who is here?", and no answer is better than a made-up one."""
    table, _rt = make_table()
    assert "The party" not in server.build_system_prompt(table).stable


def test_the_fold_in_is_given_the_party(make_table, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "ask_llm",
                        lambda *a, **k: seen.append(a) or "## Ongoing threads\n- x\n")
    table, rt = make_table()
    server.PARTY.create(table, rt, PC)
    server._summarize_ambient(table, rt, "some table talk")
    assert "Seraphine" in str(seen)


def test_an_empty_party_produces_no_prompt_text(make_table):
    table, _rt = make_table()
    assert server.PARTY.prompt_text(table) == ""


# ------------------------------------------------- never written by a machine ---

def test_a_fold_in_never_writes_the_party(make_table, monkeypatch):
    """The reason Party does not inherit Roster.apply_updates(). Nothing in the
    fold-in path may create or edit a character, and the check is on the file
    itself rather than on a mock, so a future write by any route is caught."""
    table, rt = make_table()
    server.PARTY.create(table, rt, PC)
    before = table.party_file.read_bytes()

    monkeypatch.setattr(server, "ask_llm", lambda *a, **k: (
        "## Ongoing threads\n- Something.\n\n"
        "## Cast updates\n- Seraphine: a paladin.\n\n"
        "## Location updates\n- The Mine: dark.\n\n"
        "## New events\n- Seraphine swung. {roll: character=Seraphine, outcome=success}\n"))
    server._summarize_ambient(table, rt, "seraphine did something")

    assert table.party_file.read_bytes() == before
    assert [e["name"] for e in server.PARTY.listing(table)] == ["Seraphine"]


def test_the_party_and_the_cast_are_different_files(make_table):
    table, rt = make_table()
    server.PARTY.create(table, rt, PC)
    server.CAST.apply_updates(table, rt, "- Yarrow: a hedge witch.\n", "2026-08-21")
    assert [e["name"] for e in server.PARTY.listing(table)] == ["Seraphine"]
    assert [e["name"] for e in server.CAST.listing(table)] == ["Yarrow"]


def test_the_party_has_a_lock_of_its_own(make_table):
    _table, rt = make_table()
    assert rt.party_lock is not rt.cast_lock
    assert rt.party_lock is not rt.campaign_lock


# ------------------------------------------------------------------ the API ---

def test_every_member_can_read_the_party(live):
    live.client.post(live.url("/party"), json=PC)
    player = live.player_client("Rowan")
    body = player.get(live.url("/party")).get_json()
    assert [e["name"] for e in body["party"]] == ["Seraphine"]
    assert body["mayCreate"] is False


def test_only_the_dm_can_add_a_character(live):
    player = live.player_client("Rowan")
    assert player.post(live.url("/party"), json=PC).status_code == 403
    assert live.client.post(live.url("/party"), json=PC).status_code == 201


def test_a_player_may_edit_their_own_character(live):
    """Your character is yours -- the same seam as a familiar's summoner."""
    # One client throughout: every call to player_client() joins again and
    # mints a *new* member row, so a second Rowan is a different person as far
    # as the seam is concerned.
    rowan = live.player_client("Rowan")
    rowan_id = next(m["id"] for m in live.db.members(live.table.id)
                    if m["display_name"] == "Rowan")
    live.client.post(live.url("/party"), json=dict(PC, member_id=rowan_id))

    r = rowan.put(live.url("/party/seraphine"), json={"notes": "Braver now."})
    assert r.status_code == 200 and r.get_json()["notes"] == "Braver now."


def test_a_player_may_not_edit_somebody_elses(live):
    live.client.post(live.url("/party"), json=PC)
    rowan = live.player_client("Rowan")
    r = rowan.put(live.url("/party/seraphine"), json={"notes": "Mine now."})
    assert r.status_code == 403
    assert server.PARTY.get(live.table, "seraphine")["notes"] == PC["notes"]


def test_an_unclaimed_character_is_the_dms(live):
    """Nobody gets to claim a character by being the first to ask."""
    live.client.post(live.url("/party"), json=PC)
    assert server.PARTY.get(live.table, "seraphine")["member_id"] == ""
    rowan = live.player_client("Rowan")
    assert rowan.put(live.url("/party/seraphine"), json={"notes": "x"}).status_code == 403


def test_a_player_may_not_link_a_character_to_themselves(live):
    """member_id is a permission. Granting yourself one is the thing the seam
    exists to prevent, and it is refused rather than quietly dropped."""
    rowan = live.player_client("Rowan")
    rowan_id = next(m["id"] for m in live.db.members(live.table.id)
                    if m["display_name"] == "Rowan")
    live.client.post(live.url("/party"), json=dict(PC, member_id=rowan_id))

    r = rowan.put(live.url("/party/seraphine"), json={"member_id": "somebody-else"})
    assert r.status_code == 403
    assert server.PARTY.get(live.table, "seraphine")["member_id"] == rowan_id


def test_a_dangling_link_falls_back_to_dm_only(live):
    """The player was removed from the table. Nothing cleans up the id; it just
    stops matching anybody, which is the right answer and costs no code."""
    live.client.post(live.url("/party"), json=dict(PC, member_id="gone-long-ago"))
    rowan = live.player_client("Rowan")
    assert rowan.put(live.url("/party/seraphine"), json={"notes": "x"}).status_code == 403
    assert live.client.put(live.url("/party/seraphine"), json={"notes": "x"}).status_code == 200


def test_retiring_is_not_deleting(live):
    live.client.post(live.url("/party"), json=PC)
    live.client.put(live.url("/party/seraphine"), json={"retired": True})
    entry = server.PARTY.get(live.table, "seraphine")
    assert entry is not None and entry["retired"] is True


def test_only_the_dm_can_delete(live):
    live.client.post(live.url("/party"), json=PC)
    rowan = live.player_client("Rowan")
    assert rowan.delete(live.url("/party/seraphine"), json={}).status_code == 403
    assert live.client.delete(live.url("/party/seraphine"), json={}).status_code == 200
    assert server.PARTY.get(live.table, "seraphine") is None


def test_editing_an_unknown_character_is_a_404(live):
    assert live.client.put(live.url("/party/nobody"), json={"notes": "x"}).status_code == 404
    assert live.client.delete(live.url("/party/nobody"), json={}).status_code == 404


def test_a_stranger_gets_nothing(live):
    stranger = server.app.test_client()
    assert stranger.get(live.url("/party")).status_code in (401, 403)
    assert stranger.post(live.url("/party"), json=PC).status_code in (401, 403)


def test_the_party_does_not_cross_between_tables(live):
    other_user = live.db.create_user("o@example.com", "another-long-password")
    other_id = live.db.create_table(other_user, "Blackmoor", "blackmoor")
    live.client.post(live.url("/party"), json=PC)

    from tenancy import Table
    other = Table(live.db.table(other_id), server.DATA_DIR, server.BASE)
    assert server.PARTY.listing(other) == []
