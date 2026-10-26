"""Getting your data out, and getting rid of it.

These are not features somebody asks for; they are the price of holding a
stranger's evening on a disk, and they exist before the first stranger does.

The property worth testing hardest is that deletion is *complete*. Three
stores hold a table's life -- rows in the database, files on the disk, and the
full-text index -- and only the first two are joined by a foreign key. A
"delete" that clears the rows and leaves a stranger's session logs in the
search index, or on the filesystem, is not a deletion, and it would be an easy
one to ship without noticing.
"""

import io
import zipfile

import pytest

import server


def _seed(live, text="They reached the Sunken Chapel."):
    """Give the table something to lose."""
    from datetime import datetime, timezone
    when = datetime(2026, 8, 21, 19, 0, 0, tzinfo=timezone.utc)
    server._append_timeline(live.table, live.rt, f"- {text}", when=when)
    server.CAST.apply_updates(live.table, live.rt, "- Yarrow: a hedge witch.\n", "2026-08-21")
    server._log_qa(live.table, "Where are we?", "In the chapel.")


# ------------------------------------------------------------------ export ---

def test_the_export_contains_the_tables_files(live):
    _seed(live)
    r = live.client.get(live.url("/export"))
    assert r.status_code == 200
    assert r.mimetype == "application/zip"

    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        names = z.namelist()
        assert "cast.json" in names
        assert "sessions/2026-08-21-timeline.log" in names
        assert "README.txt" in names, "an archive nobody can read is not an export"
        assert "Sunken Chapel" in z.read("sessions/2026-08-21-timeline.log").decode()


def test_the_export_is_named_for_the_table_and_the_day(live):
    r = live.client.get(live.url("/export"))
    disposition = r.headers["Content-Disposition"]
    assert "attachment" in disposition
    assert live.table.slug in disposition and ".zip" in disposition


def test_a_player_cannot_export_the_whole_table(live):
    """A player can read the record a page at a time in the browser. Walking
    out with the archive is a different act."""
    _seed(live)
    player = live.player_client("Rowan")
    assert player.get(live.url("/export")).status_code == 403


def test_a_stranger_cannot_export(live):
    stranger = server.app.test_client()
    assert stranger.get(live.url("/export")).status_code in (401, 403)


def test_exporting_an_empty_table_still_works(live):
    """A table created five minutes ago has no directory contents to speak
    of. That is not an error."""
    r = live.client.get(live.url("/export"))
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        assert z.namelist() == ["README.txt"] or "README.txt" in z.namelist()


# ------------------------------------------------------------------ delete ---

def test_deleting_a_table_removes_rows_files_and_index(live):
    """The whole point. Three stores, one act."""
    _seed(live)
    table_id, table_dir = live.table.id, live.table.dir
    assert table_dir.exists()
    assert live.db.search(table_id, "chapel")

    r = live.client.post(live.url("/delete"), json={"confirm": live.table.slug})
    assert r.status_code == 200

    assert live.db.table(table_id) is None, "row still there"
    assert not table_dir.exists(), "files still there"
    assert live.db.search(table_id, "chapel") == [], "prose still in the index"


def test_deleting_a_table_takes_its_members_with_it(live):
    live.player_client("Rowan")
    table_id = live.table.id
    assert live.db.members(table_id)

    live.client.post(live.url("/delete"), json={"confirm": live.table.slug})
    assert live.db.members(table_id) == []


def test_deletion_needs_the_name_typed_back(live):
    """The one call that destroys somebody's evenings should be hard to make
    by accident from a script, not only from a browser."""
    r = live.client.post(live.url("/delete"), json={})
    assert r.status_code == 400
    assert live.table.dir.exists()

    r = live.client.post(live.url("/delete"), json={"confirm": "something else"})
    assert r.status_code == 400
    assert live.db.table(live.table.id) is not None


def test_a_dm_who_does_not_own_the_table_cannot_delete_it(live):
    """Owner and DM are deliberately different people on this service. The
    campaign is the DM's; the table is the owner's."""
    other = live.db.create_user("dm2@example.com", "another-long-password")
    live.db.add_member(live.table.id, "Second DM", "dm", other)
    client = server.app.test_client()
    client.post("/api/login", json={"email": "dm2@example.com",
                                    "password": "another-long-password"})

    r = client.post(live.url("/delete"), json={"confirm": live.table.slug})
    assert r.status_code == 403
    assert live.db.table(live.table.id) is not None


def test_a_player_cannot_delete_the_table(live):
    player = live.player_client("Rowan")
    r = player.post(live.url("/delete"), json={"confirm": live.table.slug})
    assert r.status_code == 403
    assert live.db.table(live.table.id) is not None


def test_deleting_one_table_leaves_another_alone(live):
    """The failure that would be worst and quietest."""
    _seed(live)
    other_id = live.db.create_table(live.user_id, "Blackmoor", "blackmoor")
    from tenancy import Table
    other = Table(live.db.table(other_id), server.DATA_DIR, server.BASE)
    other.ensure_dirs()
    server.CAST.apply_updates(other, server.get_runtime(other_id),
                              "- Yarrow: a hedge witch.\n", "2026-08-21")

    live.client.post(live.url("/delete"), json={"confirm": live.table.slug})

    assert live.db.table(other_id) is not None
    assert other.dir.exists()
    assert live.db.search(other_id, "Yarrow")


# ------------------------------------------------------------------- leave ---

def test_a_player_can_leave(live):
    player = live.player_client("Rowan")
    assert player.post(live.url("/leave"), json={}).status_code == 200
    assert player.get(live.url("/cast")).status_code in (401, 403)


def test_leaving_does_not_erase_what_you_corrected(live):
    """An amendment is a statement about what happened at a table, and the
    table keeps its own history. What goes is your way back in."""
    _seed(live)
    player = live.player_client("Rowan")
    player.post(live.url("/sessions/2026-08-21/timeline/2026-08-21-0000/amend"),
                json={"text": "They reached the Sunken Barrow."})
    player.post(live.url("/leave"), json={})

    events = server._resolve_timeline_events(live.table, "2026-08-21")
    assert events[0]["current"] == "They reached the Sunken Barrow."
    assert events[0]["amendments"][-1]["by"] == "Rowan"


def test_the_owner_cannot_leave_their_own_table(live):
    r = live.client.post(live.url("/leave"), json={})
    assert r.status_code == 400
    assert "delete" in r.get_json()["error"]


def test_a_dm_cannot_walk_out_leaving_nobody_in_charge(live):
    """remove_member only removes players. A cheerful ok that removed nothing
    would be a lie."""
    other = live.db.create_user("dm2@example.com", "another-long-password")
    live.db.add_member(live.table.id, "Second DM", "dm", other)
    client = server.app.test_client()
    client.post("/api/login", json={"email": "dm2@example.com",
                                    "password": "another-long-password"})

    r = client.post(live.url("/leave"), json={})
    assert r.status_code == 400
    assert live.db.member_for_user(live.table.id, other) is not None


# ----------------------------------------------------------------- account ---

def test_deleting_an_account_takes_the_tables_it_owns(live):
    _seed(live)
    table_id, table_dir = live.table.id, live.table.dir

    r = live.client.post("/api/account/delete",
                         json={"password": "a-long-enough-password"})
    assert r.status_code == 200
    assert r.get_json()["tables_deleted"] == 1
    assert live.db.user(live.user_id) is None
    assert live.db.table(table_id) is None
    assert not table_dir.exists()
    assert live.db.search(table_id, "chapel") == []


def test_deleting_an_account_needs_the_password(live):
    r = live.client.post("/api/account/delete", json={"password": "wrong"})
    assert r.status_code == 403
    assert live.db.user(live.user_id) is not None


def test_deleting_an_account_leaves_other_peoples_tables_alone(live):
    """A table you merely play at is not yours to destroy.

    Checks the *files* as well as the row, and that is not belt-and-braces.
    tables_for_user() returns every table you can open, owned or not; the row
    is protected by the owner_id in the SQL, but the directories are deleted by
    a separate loop in Python. A version of that loop without its ownership
    filter passes every other test in this file while quietly erasing somebody
    else's campaign from the disk and leaving a row pointing at nothing.
    """
    other_user = live.db.create_user("other@example.com", "another-long-password")
    other_id = live.db.create_table(other_user, "Blackmoor", "blackmoor")
    live.db.add_member(other_id, "Guest", "player", live.user_id)

    from tenancy import Table
    other = Table(live.db.table(other_id), server.DATA_DIR, server.BASE)
    other.ensure_dirs()
    (other.dir / "campaign.md").write_text("Not yours.", encoding="utf-8")
    server.CAST.apply_updates(other, server.get_runtime(other_id),
                              "- Yarrow: a hedge witch.\n", "2026-08-21")

    live.client.post("/api/account/delete", json={"password": "a-long-enough-password"})

    assert live.db.table(other_id) is not None, "their row"
    assert other.dir.exists(), "their files"
    assert (other.dir / "campaign.md").read_text() == "Not yours."
    assert live.db.search(other_id, "Yarrow"), "their search index"


def test_a_signed_out_visitor_cannot_delete_an_account(live):
    stranger = server.app.test_client()
    assert stranger.post("/api/account/delete", json={"password": "x"}).status_code == 401


# ---------------------------------------------------- the doors on the way in ---

def test_a_stranger_at_the_root_is_sent_to_sign_in(live):
    stranger = server.app.test_client()
    r = stranger.get("/")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/login.html")


def test_a_fresh_install_sends_the_first_visitor_to_sign_up(tmp_path, monkeypatch):
    """Nobody has an account yet, so there is nobody to sign in as: the first
    person through the door is the one who sets the install up."""
    import db as db_module
    monkeypatch.setattr(server, "db", db_module.Database(tmp_path / "empty.db"))
    r = server.app.test_client().get("/")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/signup.html")


def test_the_privacy_page_needs_no_account(live):
    stranger = server.app.test_client()
    assert stranger.get("/privacy.html").status_code == 200


def test_the_invitation_page_says_what_recording_means(live):
    """Consent cannot be given by a DM on behalf of five other people, and the
    invitation is the one door every one of them walks through."""
    import re
    stranger = server.app.test_client()
    raw = stranger.get("/join.html").get_data(as_text=True)
    # Collapse whitespace: the phrasing matters, the line wrapping does not,
    # and a test that pins prose to a column width gets edited into silence.
    body = re.sub(r"\s+", " ", raw)
    assert "record table talk" in body, "the notice must name the switch people see"
    assert "/privacy.html" in body
    assert 'id="consent"' in raw and "required" in raw
