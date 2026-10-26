"""Who may change a familiar.

Most tools of this kind assume the DM buys the service and therefore controls
everything in it. This one does not: in the fiction a familiar is summoned by a
*player*, and the person paying may well be that player rather than the DM. So
ownership splits along the same seam the memory already splits along --

    the creature   persona, bond, voice, whether it is bound   -> its summoner
    the campaign   notes, cast, timeline, invitations, bill    -> the DM

and the DM is *not* permitted on the creature's side. What a player made their
familiar, and what they have told it, is between the two of them; a DM who can
open the persona can read that. The DM keeps exactly two powers over somebody
else's familiar -- dismissing it, and handing it to another player -- because a
creature whose summoner has left the group has to be clearable by somebody, and
who sits at the table is the DM's business in a way that a persona is not.
"""

import pytest

import server


DM = {"id": "m-dm", "role": "dm"}
ROWAN = {"id": "m-rowan", "role": "player"}
OTHER = {"id": "m-other", "role": "player"}


@pytest.fixture
def owned(live):
    """A table where Rowan has summoned Pip and the DM has summoned Grix."""
    rowan = live.player_client("Rowan")
    rowan_member = live.db.members(live.table.id)[-1]
    dm_member = live.db.member_for_user(live.table.id, live.user_id)

    fams = live.table.familiars
    fams.create("Pip", owner=rowan_member["id"], persona="You are Pip.")
    fams.create("Grix", owner=dm_member["id"], persona="You are Grix.")
    fams.set_active("pip")

    live.rowan = rowan
    live.rowan_id = rowan_member["id"]
    live.dm_member_id = dm_member["id"]
    return live


# ------------------------------------------------------------ the predicate --

@pytest.mark.parametrize("member,expected", [
    (ROWAN, True),    # the summoner
    (DM, False),      # not even the DM -- a persona is not theirs to read
    (OTHER, False),   # another player at the same table
    (None, False),
])
def test_may_edit(make_table, member, expected):
    table, _rt = make_table()
    pip = table.familiars.create("Pip", owner="m-rowan")
    assert pip.may_edit(member) is expected


@pytest.mark.parametrize("member,expected", [
    (ROWAN, True),    # the summoner
    (DM, True),       # the escape hatch: a summoner who has left the group
    (OTHER, False),
    (None, False),
])
def test_may_retire(make_table, member, expected):
    """The one seam where the DM is permitted and may_edit is not."""
    table, _rt = make_table()
    pip = table.familiars.create("Pip", owner="m-rowan")
    assert pip.may_retire(member) is expected


def test_an_unowned_familiar_stays_dm_only(make_table):
    """Familiars that predate ownership must not loosen their permissions just
    by being loaded by a newer build."""
    table, _rt = make_table()
    legacy = table.familiars.create("Legacy")
    assert legacy.owner == ""
    assert legacy.may_edit(DM) is True
    assert legacy.may_edit(ROWAN) is False


def test_ownership_survives_a_round_trip(make_table):
    table, _rt = make_table()
    table.familiars.create("Pip", owner="m-rowan")
    assert table.familiars.get("pip").owner == "m-rowan"


# ---------------------------------------------------------------- persona ----

def test_the_summoner_may_read_and_write_their_familiars_persona(owned):
    r = owned.rowan.get(owned.url("/familiars/pip/persona"))
    assert r.status_code == 200
    assert "You are Pip" in r.get_json()["persona"]

    assert owned.rowan.put(owned.url("/familiars/pip/persona"),
                           json={"persona": "You are Pip, and braver now."}).status_code == 200
    assert "braver now" in owned.table.familiars.get("pip").persona_path.read_text()


def test_another_player_may_not(owned):
    intruder = owned.player_client("Nosy")
    assert intruder.get(owned.url("/familiars/pip/persona")).status_code == 403
    assert intruder.put(owned.url("/familiars/pip/persona"),
                        json={"persona": "hacked"}).status_code == 403
    assert "hacked" not in owned.table.familiars.get("pip").persona_path.read_text()


def test_the_dm_may_not_read_a_players_persona(owned):
    """The DM runs the game; they do not get to read what a player told their
    familiar in confidence. Reading is refused as firmly as writing -- a 403 on
    the PUT alone would still leak the text through the GET the editor uses."""
    assert owned.client.get(owned.url("/familiars/pip/persona")).status_code == 403
    assert owned.client.put(owned.url("/familiars/pip/persona"),
                            json={"persona": "You are the DM's now."}).status_code == 403
    assert "DM's now" not in owned.table.familiars.get("pip").persona_path.read_text()


def test_the_dm_may_still_dismiss_an_abandoned_familiar(owned):
    """The escape hatch, and the reason may_retire exists at all: Rowan leaves,
    Pip is editable by nobody, and the table must not be stuck with it."""
    assert owned.client.delete(owned.url("/familiars/pip"), json={}).status_code == 200
    assert owned.table.familiars.get("pip") is None


def test_the_dm_may_hand_a_familiar_to_somebody_else(owned):
    """Reassignment is the other half of the escape hatch. Who a creature
    belongs to is a fact about the table; what it is, is not."""
    other = owned.player_client("Sam")
    sam = next(m["id"] for m in owned.db.members(owned.table.id)
               if m["display_name"] == "Sam")

    assert owned.client.patch(owned.url("/familiars/pip"),
                              json={"owner": sam}).status_code == 200
    assert owned.table.familiars.get("pip").owner == sam
    # Still not a way in: handing it over does not hand over the persona.
    assert owned.client.get(owned.url("/familiars/pip/persona")).status_code == 403
    assert other.get(owned.url("/familiars/pip/persona")).status_code == 200


def test_the_dm_may_not_rename_somebody_elses_familiar(owned):
    """Reassignment is permitted because it is administrative. Everything else
    on the same route is not -- otherwise `owner` would be a way in."""
    r = owned.client.patch(owned.url("/familiars/pip"), json={"name": "Mine"})
    assert r.status_code == 403
    assert owned.table.familiars.get("pip").name == "Pip"


def test_a_summoner_reaches_their_own_familiar_while_another_is_bound(owned):
    """The bug the route split exists to make impossible.

    Rowan summoned Pip; Grix is on screen. The old route resolved active() and
    *then* checked edit rights, so Rowan was refused with "Grix was summoned by
    someone else" and could never reach their own creature. Addressing a
    familiar by id removes the question entirely."""
    owned.table.familiars.set_active("grix")
    assert owned.table.familiars.active().id == "grix"

    r = owned.rowan.get(owned.url("/familiars/pip/persona"))
    assert r.status_code == 200 and "You are Pip" in r.get_json()["persona"]
    assert owned.rowan.put(owned.url("/familiars/pip/persona"),
                           json={"persona": "Still Pip."}).status_code == 200


def test_a_stale_persona_revision_is_refused(owned):
    body = owned.rowan.get(owned.url("/familiars/pip/persona")).get_json()
    owned.rowan.put(owned.url("/familiars/pip/persona"),
                    json={"persona": "Theirs.", "revision": body["revision"]})
    r = owned.rowan.put(owned.url("/familiars/pip/persona"),
                        json={"persona": "Mine.", "revision": body["revision"]})
    assert r.status_code == 409
    assert r.get_json()["persona"] == "Theirs."
    assert "Mine." not in owned.table.familiars.get("pip").persona_path.read_text()


def test_an_unknown_familiar_is_a_404(owned):
    assert owned.client.get(owned.url("/familiars/nobody/persona")).status_code == 404


def test_the_campaign_notes_stay_the_dms_even_for_the_summoner(owned):
    """The familiar is Rowan's; the campaign is not. The seam moved routes but
    it did not move: this is role="dm", not _may_edit_familiar."""
    r = owned.rowan.put(owned.url("/campaign"), json={"fields": {"where": "In the mine."}})
    assert r.status_code == 403
    assert not (owned.table.dir / "campaign.md").exists()

    assert owned.client.put(owned.url("/campaign"),
                            json={"fields": {"where": "In the mine."}}).status_code == 200


def test_the_editor_is_told_which_half_it_may_change(owned):
    assert owned.rowan.get(owned.url("/campaign")).get_json()["mayEdit"] is False
    assert owned.client.get(owned.url("/campaign")).get_json()["mayEdit"] is True


def test_the_old_persona_route_is_gone(owned):
    """Retired rather than fixed. It resolved active() and then checked rights
    on it, which is what made a summoner unable to reach their own familiar --
    a shape that cannot be made correct, only replaced."""
    assert owned.client.get(owned.url("/persona")).status_code == 404
    assert owned.client.put(owned.url("/persona"),
                            json={"persona": "x"}).status_code in (404, 405)


# ------------------------------------------------------------------ voice ----

def test_the_summoner_may_change_their_familiars_voice(owned, monkeypatch):
    monkeypatch.setattr(server, "voice_exists", lambda name: True)
    assert owned.rowan.post(owned.url("/voice"), json={"voice": "reedy"}).status_code == 200
    assert owned.table.familiars.get("pip").voice == "reedy"


def test_a_player_may_not_revoice_someone_elses_familiar(owned, monkeypatch):
    monkeypatch.setattr(server, "voice_exists", lambda name: True)
    owned.table.familiars.set_active("grix")          # the DM's familiar
    assert owned.rowan.post(owned.url("/voice"), json={"voice": "reedy"}).status_code == 403
    assert owned.table.familiars.get("grix").voice == ""


# ---------------------------------------------------------------- binding ----

def test_a_player_may_summon_their_own_familiar(owned):
    owned.table.familiars.set_active("grix")
    r = owned.rowan.post(owned.url("/familiar"), json={"id": "pip"})
    assert r.status_code == 200
    assert owned.table.familiars.active().id == "pip"


def test_a_player_may_not_summon_someone_elses(owned):
    """Checked against the familiar being summoned, not the one already bound --
    otherwise the answer would depend on whose creature happened to be present."""
    r = owned.rowan.post(owned.url("/familiar"), json={"id": "grix"})
    assert r.status_code == 403
    assert owned.table.familiars.active().id == "pip"


def test_binding_an_unknown_familiar_is_a_404_not_a_403(owned):
    assert owned.rowan.post(owned.url("/familiar"), json={"id": "nobody"}).status_code == 404


# ----------------------------------------------------------------- config ----

def test_config_tells_the_browser_who_it_is_and_who_owns_what(owned):
    body = owned.rowan.get(owned.url("/config")).get_json()
    assert body["me"]["id"] == owned.rowan_id
    assert body["me"]["role"] == "player"
    owners = {f["name"]: f["owner"] for f in body["familiars"]}
    assert owners == {"Pip": owned.rowan_id, "Grix": owned.dm_member_id}


# ------------------------------------------------------- owner is not DM ----

def test_a_table_can_be_owned_by_someone_who_is_not_the_dm(live):
    """The whole point: the buyer may be the player who summoned the familiar.
    `tables.owner_id` is the billing relationship; `members.role` is the job."""
    buyer = live.db.create_user("player@example.com", "a-long-enough-password")
    table_id = live.db.create_table(buyer, "Rowan's table", "rowans", role="player")

    assert live.db.table(table_id)["owner_id"] == buyer
    assert live.db.member_for_user(table_id, buyer)["role"] == "player"


def test_the_default_is_still_that_the_creator_dms(live):
    table_id = live.db.create_table(live.user_id, "Ordinary", "ordinary")
    assert live.db.member_for_user(table_id, live.user_id)["role"] == "dm"


def test_an_unknown_role_is_refused(live):
    with pytest.raises(ValueError):
        live.db.create_table(live.user_id, "Bad", "bad", role="overlord")


def test_a_table_created_through_the_api_gives_its_familiar_an_owner(live):
    r = live.client.post("/api/tables",
                         json={"name": "New table", "slug": "newtable", "familiar": "Sable"})
    assert r.status_code == 200

    from tenancy import Table
    made = Table(live.db.table_by_slug("newtable"), server.DATA_DIR, server.BASE)
    creator = live.db.member_for_user(made.id, live.user_id)
    assert made.familiars.active().owner == creator["id"]


# ------------------------------------------------------------ DM secrets ----

def test_a_player_reads_the_notes_but_not_the_secrets(owned):
    """The behaviour that changed, and why. The whole document used to be
    withheld from anyone but the DM -- the only safe move while house rules and
    secrets shared one file. They no longer do, so the notes are readable (the
    familiar recites them aloud anyway) and only the secrets are withheld.

    `dmOnly` is *absent* rather than null: a key that is sometimes a secret and
    sometimes an empty string is a key somebody eventually forgets to check."""
    (owned.table.dir / "campaign.md").write_text(
        "## Where we are\n\nThe party are in Greyhaven.\n", encoding="utf-8")
    owned.table.dm_only_file.write_text("The innkeeper is a doppelganger.\n",
                                        encoding="utf-8")

    body = owned.rowan.get(owned.url("/campaign")).get_json()
    assert body["fields"]["where"] == "The party are in Greyhaven."
    assert "dmOnly" not in body
    assert "doppelganger" not in str(body)


def test_the_dm_gets_the_secrets_too(owned):
    owned.table.dm_only_file.write_text("secret\n", encoding="utf-8")
    body = owned.client.get(owned.url("/campaign")).get_json()
    assert "secret" in body["dmOnly"]


def test_a_secret_left_in_the_campaign_file_is_shown_in_the_secret_box(owned):
    """A DM types "## DM only" out of habit. It must surface where it belongs
    so that saving the form is what finally moves it -- and it must not reach
    a player on the way."""
    (owned.table.dir / "campaign.md").write_text(
        "## Where we are\n\nGreyhaven.\n\n## DM only\n\nThe innkeeper lies.\n",
        encoding="utf-8")

    assert "innkeeper" in owned.client.get(owned.url("/campaign")).get_json()["dmOnly"]
    assert "innkeeper" not in str(owned.rowan.get(owned.url("/campaign")).get_json())
