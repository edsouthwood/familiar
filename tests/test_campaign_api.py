"""The campaign-notes endpoints.

The parser has its own suite (test_campaign.py) and the secrets have theirs
(test_dm_only.py). What is left here is the route: who may read, who may write,
and what happens when two people edit the same notes.

That last one is not exotic. It is one person on a laptop and the same person on
the tablet they left open at last week's session — and without a revision check
the second save silently discards the first, with nobody ever finding out.
"""

import pytest

import campaign as campaign_notes
import server


def _fields(live):
    return live.client.get(live.url("/campaign")).get_json()


# ------------------------------------------------------------------ reading ---

def test_a_new_table_has_empty_fields_not_a_missing_key(live):
    body = _fields(live)
    assert set(body["fields"]) == set(campaign_notes.FIELD_KEYS)
    assert all(value == "" for value in body["fields"].values())


def test_the_labels_come_from_the_server(live):
    """So a field cannot be renamed in the form and stay the old name in the
    file — one source of truth for what each box is called."""
    assert _fields(live)["labels"]["where"] == "Where we are"


def test_a_player_may_read_the_notes(live):
    live.client.put(live.url("/campaign"), json={"fields": {"where": "In the mine."}})
    player = live.player_client("Rowan")
    body = player.get(live.url("/campaign")).get_json()
    assert body["fields"]["where"] == "In the mine."
    assert body["mayEdit"] is False


def test_a_stranger_may_not(live):
    stranger = server.app.test_client()
    assert stranger.get(live.url("/campaign")).status_code in (401, 403)


# ------------------------------------------------------------------ writing ---

def test_saving_writes_a_file_a_person_can_read(live):
    live.client.put(live.url("/campaign"),
                    json={"fields": {"where": "In the mine.",
                                     "house_rules": "- Luck rolls are a thing"}})
    text = (live.table.dir / "campaign.md").read_text()
    assert "## Where we are" in text and "In the mine." in text
    assert "## House rules" in text


def test_the_response_carries_the_re_parsed_fields(live):
    """Not the submitted ones. The box should show what the file now says, so
    a save doubles as the reload that used to be missing."""
    r = live.client.put(live.url("/campaign"),
                        json={"fields": {"where": "  In the mine.  "}})
    assert r.get_json()["fields"]["where"] == "In the mine."


def test_an_omitted_field_is_left_alone(live):
    """A partial save must not blank the fields it did not mention."""
    live.client.put(live.url("/campaign"),
                    json={"fields": {"where": "In the mine.", "setting": "Westmarch."}})
    live.client.put(live.url("/campaign"), json={"fields": {"where": "In the tunnel."}})
    body = _fields(live)
    assert body["fields"]["where"] == "In the tunnel."
    assert body["fields"]["setting"] == "Westmarch."


def test_a_field_can_be_deliberately_emptied(live):
    """Distinct from the test above: an explicit "" is a decision, and must
    not be mistaken for an omission."""
    live.client.put(live.url("/campaign"), json={"fields": {"setting": "Westmarch."}})
    live.client.put(live.url("/campaign"), json={"fields": {"setting": ""}})
    assert _fields(live)["fields"]["setting"] == ""


def test_an_unknown_field_is_refused(live):
    r = live.client.put(live.url("/campaign"), json={"fields": {"secrets": "oops"}})
    assert r.status_code == 400
    assert "secrets" in r.get_json()["error"]


def test_a_non_string_field_is_refused(live):
    assert live.client.put(live.url("/campaign"),
                           json={"fields": {"where": ["a", "list"]}}).status_code == 400


def test_fields_must_be_an_object(live):
    assert live.client.put(live.url("/campaign"),
                           json={"fields": "just a string"}).status_code == 400


def test_a_player_may_not_write(live):
    player = live.player_client("Rowan")
    r = player.put(live.url("/campaign"), json={"fields": {"where": "mine"}})
    assert r.status_code == 403
    assert not (live.table.dir / "campaign.md").exists()


# ------------------------------------------------------------ two editors ---

def test_a_stale_revision_is_refused(live):
    """The whole point of the revision. Without this the second save wins and
    the first person's paragraph is gone with no error anywhere."""
    first = _fields(live)
    live.client.put(live.url("/campaign"),
                    json={"fields": {"where": "Written by the laptop."},
                          "revision": first["revision"]})

    r = live.client.put(live.url("/campaign"),
                        json={"fields": {"where": "Written by the tablet."},
                              "revision": first["revision"]})
    assert r.status_code == 409
    assert (live.table.dir / "campaign.md").read_text().count("laptop") == 1
    assert "tablet" not in (live.table.dir / "campaign.md").read_text()


def test_a_conflict_hands_back_the_servers_version(live):
    """So the client can show both rather than picking a winner on the user's
    behalf — which is the only thing it can honestly do."""
    first = _fields(live)
    live.client.put(live.url("/campaign"),
                    json={"fields": {"where": "Theirs."}, "revision": first["revision"]})
    r = live.client.put(live.url("/campaign"),
                        json={"fields": {"where": "Mine."}, "revision": first["revision"]})
    assert r.get_json()["fields"]["where"] == "Theirs."
    assert r.get_json()["revision"] != first["revision"]


def test_a_fresh_revision_is_accepted(live):
    body = _fields(live)
    r = live.client.put(live.url("/campaign"),
                        json={"fields": {"where": "Here."}, "revision": body["revision"]})
    assert r.status_code == 200
    assert r.get_json()["revision"] != body["revision"]


def test_omitting_the_revision_accepts_whatever_is_there(live):
    """What a script wants, and what a form must never send."""
    live.client.put(live.url("/campaign"), json={"fields": {"where": "First."}})
    r = live.client.put(live.url("/campaign"), json={"fields": {"where": "Second."}})
    assert r.status_code == 200


def test_the_revision_tracks_the_secrets_too(live):
    """One form saves both files, so a change to either has to invalidate it —
    otherwise editing the secret box on one device is silently lost by a save
    from the other."""
    before = _fields(live)["revision"]
    live.table.dm_only_file.write_text("a new secret\n", encoding="utf-8")
    assert _fields(live)["revision"] != before


# ------------------------------------------------------------- the prompt ---

def test_saved_notes_reach_the_familiar(live):
    """End to end, because every layer in between is only worth having if this
    holds."""
    live.client.put(live.url("/campaign"),
                    json={"fields": {"where": "In the flooded chapel."}})
    assert "In the flooded chapel." in server.build_system_prompt(live.table).stable


def test_saved_notes_reach_the_fold_in(live):
    live.client.put(live.url("/campaign"), json={"fields": {"where": "In the mine."}})
    assert "In the mine." in server._campaign_reference(live.table)


def test_spoken_prompt_ends_with_the_reminder_outside_the_cache(live):
    """Answers get the speaking reminder as the last thing the model reads, in
    the volatile half so the cached prefix is the same as everyone else's."""
    plain = server.build_system_prompt(live.table)
    spoken = server.build_spoken_prompt(live.table)
    assert spoken.stable == plain.stable
    assert spoken.volatile.endswith(server.SPOKEN_REPLY_REMINDER)
    assert server.SPOKEN_REPLY_REMINDER not in plain.text
