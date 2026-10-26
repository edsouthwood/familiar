"""Running a table: who is at it, and which creatures it keeps.

Both halves were reachable only through the database before this — `db.py` had
`rename_member` and `remove_member` with no route calling either, and a
familiar could be created exactly once, at table creation, with no way to add,
rename or dismiss one afterwards. The owl shell has shipped for weeks with no
way to choose it.

The permission questions are the interesting part, and they are not the same
question twice:

  members    the DM's, always — a display name is the attribution on a
             permanent record, so renaming somebody edits the table's history
  familiars  the summoner's, or the DM's — a creature belongs to whoever
             called it, except that creating one is DM-only because anyone who
             can create can also bind, and binding takes over the screen
"""

import pytest

import server


# ------------------------------------------------------------------ members ---

def test_the_dm_sees_who_is_at_the_table(live):
    live.player_client("Rowan")
    body = live.client.get(live.url("/members")).get_json()
    names = {m["name"]: m for m in body["members"]}
    assert "Rowan" in names and names["Rowan"]["role"] == "player"
    assert names["Rowan"]["has_account"] is False


def test_the_member_list_is_not_the_invite_link(live):
    """Split apart on purpose. A page that showed both could not be put on
    screen while somebody was looking over your shoulder, which is exactly
    when you want to check who is here."""
    body = live.client.get(live.url("/members")).get_json()
    assert "url" not in body and live.table.invite_token not in str(body)


def test_a_player_may_not_see_the_member_list(live):
    player = live.player_client("Rowan")
    assert player.get(live.url("/members")).status_code == 403


def test_the_dm_can_rename_somebody(live):
    live.player_client("Rowan")
    rowan = next(m["id"] for m in live.db.members(live.table.id)
                 if m["display_name"] == "Rowan")
    assert live.client.patch(live.url(f"/members/{rowan}"),
                             json={"name": "Rowan the Bold"}).status_code == 200
    assert live.db.member(rowan)["display_name"] == "Rowan the Bold"


def test_a_player_may_not_rename_themselves(live):
    """Not pedantry. The display name is the `by` on every correction that
    person has ever made, so a self-rename rewrites the attribution on the
    table's permanent record."""
    player = live.player_client("Rowan")
    rowan = next(m["id"] for m in live.db.members(live.table.id)
                 if m["display_name"] == "Rowan")
    assert player.patch(live.url(f"/members/{rowan}"),
                        json={"name": "Rowan the Bold"}).status_code == 403
    assert live.db.member(rowan)["display_name"] == "Rowan"


def test_an_empty_name_is_refused(live):
    live.player_client("Rowan")
    rowan = next(m["id"] for m in live.db.members(live.table.id)
                 if m["display_name"] == "Rowan")
    assert live.client.patch(live.url(f"/members/{rowan}"),
                             json={"name": "   "}).status_code == 400


def test_removing_somebody_takes_their_way_back_in(live):
    player = live.player_client("Rowan")
    rowan = next(m["id"] for m in live.db.members(live.table.id)
                 if m["display_name"] == "Rowan")
    assert player.get(live.url("/cast")).status_code == 200

    assert live.client.delete(live.url(f"/members/{rowan}"), json={}).status_code == 200
    assert player.get(live.url("/cast")).status_code in (401, 403)


def test_removing_somebody_leaves_their_corrections_alone(live):
    """An amendment is a statement about what happened at a table, and the
    table keeps its own history."""
    from datetime import datetime, timezone
    server._append_timeline(live.table, live.rt, "- They crossed the bridge.",
                            when=datetime(2026, 8, 21, 19, tzinfo=timezone.utc))
    player = live.player_client("Rowan")
    player.post(live.url("/sessions/2026-08-21/timeline/2026-08-21-0000/amend"),
                json={"text": "They crossed the causeway."})
    rowan = next(m["id"] for m in live.db.members(live.table.id)
                 if m["display_name"] == "Rowan")
    live.client.delete(live.url(f"/members/{rowan}"), json={})

    events = server._resolve_timeline_events(live.table, "2026-08-21")
    assert events[0]["current"] == "They crossed the causeway."
    assert events[0]["amendments"][-1]["by"] == "Rowan"


def test_a_removed_members_familiar_is_readable_by_nobody(live):
    """Nothing cleans the dangling owner id up, and nothing needs to:
    Familiar.may_edit requires a *matching* owner, so a familiar whose summoner
    has gone becomes editable by no one -- the DM included. Leaving the group
    does not publish what you told your familiar.

    Which is precisely why may_retire exists: the table would otherwise be
    stuck with a creature nobody can touch."""
    live.player_client("Rowan")
    rowan = next(m["id"] for m in live.db.members(live.table.id)
                 if m["display_name"] == "Rowan")
    live.table.familiars.create("Pip", owner=rowan)
    live.table.familiars.create("Grix")     # so dismissing Pip is not the last one
    live.client.delete(live.url(f"/members/{rowan}"), json={})

    assert live.client.get(live.url("/familiars/pip/persona")).status_code == 403
    # The two ways out, both the DM's: hand it to somebody still here, or
    # dismiss it.
    sam = live.player_client("Sam") and next(
        m["id"] for m in live.db.members(live.table.id) if m["display_name"] == "Sam")
    assert live.client.patch(live.url("/familiars/pip"),
                             json={"owner": sam}).status_code == 200
    assert live.client.delete(live.url("/familiars/pip"), json={}).status_code == 200


def test_the_dm_cannot_be_removed(live):
    """A table with nobody to run it cannot be administered. Deleting the
    table is the honest way to end it."""
    dm = next(m["id"] for m in live.db.members(live.table.id) if m["role"] == "dm")
    r = live.client.delete(live.url(f"/members/{dm}"), json={})
    assert r.status_code == 400
    assert live.db.member_in_table(live.table.id, dm) is not None


# ---------------------------------------------------------------- familiars ---

def test_the_dm_can_summon_a_new_familiar(live):
    r = live.client.post(live.url("/familiars"), json={"name": "Grix"})
    assert r.status_code == 201
    assert r.get_json()["name"] == "Grix"
    assert live.table.familiars.get("grix") is not None


def test_a_new_familiar_answers_to_its_own_name(live):
    live.client.post(live.url("/familiars"), json={"name": "Grix"})
    assert live.table.familiars.get("grix").wake_words == ["grix"]


def test_a_player_may_not_summon_one(live):
    """Against the fiction, and deliberately. Binding is checked against the
    familiar being summoned, so anyone who can create one can bind it — which
    is to say take over the table's screen mid-session."""
    player = live.player_client("Rowan")
    assert player.post(live.url("/familiars"), json={"name": "Grix"}).status_code == 403


def test_the_dm_can_hand_a_familiar_to_a_player(live):
    """The fiction survives: the DM says "yes, Rowan has an owl", and from
    then on it is Rowan's to edit."""
    player = live.player_client("Rowan")
    rowan = next(m["id"] for m in live.db.members(live.table.id)
                 if m["display_name"] == "Rowan")
    live.client.post(live.url("/familiars"), json={"name": "Grix", "owner": rowan})

    assert live.table.familiars.get("grix").owner == rowan
    assert player.put(live.url("/familiars/grix/persona"),
                      json={"persona": "You are Grix."}).status_code == 200


def test_a_familiar_cannot_be_handed_to_a_stranger(live):
    r = live.client.post(live.url("/familiars"),
                         json={"name": "Grix", "owner": "not-a-member"})
    assert r.status_code == 400


def test_a_duplicate_name_is_refused_rather_than_silently_reused(live):
    """Familiars.create() is idempotent on id, so without this check the DM
    would be told "created" and handed back the familiar they already had."""
    live.client.post(live.url("/familiars"), json={"name": "Grix"})
    r = live.client.post(live.url("/familiars"), json={"name": "grix"})
    assert r.status_code == 409


def test_there_is_a_cap(live):
    for n in range(server.MAX_FAMILIARS_PER_TABLE):
        live.client.post(live.url("/familiars"), json={"name": f"Creature{n}"})
    r = live.client.post(live.url("/familiars"), json={"name": "One more"})
    assert r.status_code == 400


def test_a_summoner_can_rename_and_rewake_their_familiar(live):
    player = live.player_client("Rowan")
    rowan = next(m["id"] for m in live.db.members(live.table.id)
                 if m["display_name"] == "Rowan")
    live.client.post(live.url("/familiars"), json={"name": "Grix", "owner": rowan})

    r = player.patch(live.url("/familiars/grix"),
                     json={"name": "Grixel", "wake_words": "grixel, grix"})
    assert r.status_code == 200
    familiar = live.table.familiars.get("grix")
    assert familiar.name == "Grixel"
    assert familiar.wake_words == ["grixel", "grix"]
    assert familiar.id == "grix", "the id is the directory; a rename must not move it"


def test_a_familiar_needs_at_least_one_wake_word(live):
    """An empty list leaves a creature that cannot be woken by name, and
    nothing on the screen would explain why."""
    live.client.post(live.url("/familiars"), json={"name": "Grix"})
    assert live.client.patch(live.url("/familiars/grix"),
                             json={"wake_words": " , "}).status_code == 400


def test_the_shell_can_finally_be_chosen(live):
    """static/shells/owl has shipped for weeks with no way to pick it."""
    live.client.post(live.url("/familiars"), json={"name": "Grix"})
    assert live.client.patch(live.url("/familiars/grix"),
                             json={"shell": "owl"}).status_code == 200
    assert live.table.familiars.get("grix").shell == "owl"


def test_an_unknown_shell_is_refused(live):
    live.client.post(live.url("/familiars"), json={"name": "Grix"})
    assert live.client.patch(live.url("/familiars/grix"),
                             json={"shell": "dragon"}).status_code == 400


def test_only_the_dm_can_reassign_a_familiar(live):
    """`owner` decides who may edit, so setting it is granting a permission --
    refused rather than quietly dropped."""
    player = live.player_client("Rowan")
    rowan = next(m["id"] for m in live.db.members(live.table.id)
                 if m["display_name"] == "Rowan")
    live.client.post(live.url("/familiars"), json={"name": "Grix", "owner": rowan})

    r = player.patch(live.url("/familiars/grix"), json={"owner": ""})
    assert r.status_code == 403
    assert live.table.familiars.get("grix").owner == rowan


def test_another_player_may_not_touch_it(live):
    live.client.post(live.url("/familiars"), json={"name": "Grix"})
    intruder = live.player_client("Nosy")
    assert intruder.patch(live.url("/familiars/grix"),
                          json={"name": "Mine"}).status_code == 403
    assert intruder.delete(live.url("/familiars/grix"), json={}).status_code == 403


def test_a_familiar_can_be_dismissed(live):
    live.client.post(live.url("/familiars"), json={"name": "Whiskers"})
    live.client.post(live.url("/familiars"), json={"name": "Grix"})
    assert live.client.delete(live.url("/familiars/grix"), json={}).status_code == 200
    assert live.table.familiars.get("grix") is None
    assert live.table.familiars.get("whiskers") is not None


def test_the_last_familiar_cannot_be_dismissed(live):
    """A table with no familiar is a screen with nothing on it and no route
    back."""
    live.client.post(live.url("/familiars"), json={"name": "Whiskers"})
    r = live.client.delete(live.url("/familiars/whiskers"), json={})
    assert r.status_code == 400
    assert live.table.familiars.get("whiskers") is not None


def test_dismissing_the_bound_one_leaves_the_table_with_somebody(live):
    """active() falls back to the first remaining familiar, so there is never
    a moment with nobody on screen."""
    live.client.post(live.url("/familiars"), json={"name": "Whiskers"})
    live.client.post(live.url("/familiars"), json={"name": "Grix"})
    live.client.post(live.url("/familiar"), json={"id": "grix"})
    assert live.table.familiars.active().id == "grix"

    live.client.delete(live.url("/familiars/grix"), json={})
    assert live.table.familiars.active() is not None


def test_dismissing_a_familiar_takes_its_persona_with_it(live):
    live.client.post(live.url("/familiars"), json={"name": "Whiskers"})
    live.client.post(live.url("/familiars"), json={"name": "Grix"})
    live.client.put(live.url("/familiars/grix/persona"),
                    json={"persona": "You are Grix."})
    grix_dir = live.table.familiars.get("grix").dir
    live.client.delete(live.url("/familiars/grix"), json={})
    assert not grix_dir.exists()


def test_the_shipped_libraries_are_listed(live):
    body = live.client.get(live.url("/shells")).get_json()
    assert "cat" in body["shells"] and "owl" in body["shells"]
    assert "cat" in body["sounds"]


# ------------------------------------------------------------------- routing ---

def test_every_table_page_is_behind_the_membership_check(live):
    """The failure this guards is a new page served by Flask's static handler
    at the site root, outside the check entirely."""
    stranger = server.app.test_client()
    for page in server.TABLE_PAGES:
        r = stranger.get(live.url_page(page))
        assert r.status_code in (302, 401, 403), page
