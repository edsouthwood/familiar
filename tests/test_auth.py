"""Who may open a table, and who may only look.

Phase 1 removes the basic_auth Caddy put in front of the whole site, so these
checks are now the only thing between a stranger and somebody's campaign. They
are worth more than the usual "does the happy path work" coverage.
"""

import pytest

import server
from auth import member_cookie


# ------------------------------------------------------------- accounts ----

# These three are about what signup *does* -- the cookie it sets, the inputs it
# refuses. Who is allowed to reach it at all is a separate question, tested in
# test_signup_gate.py, so they open the gate explicitly rather than depending on
# whatever its default happens to be.
@pytest.fixture
def signup_open(monkeypatch):
    monkeypatch.setenv("SIGNUP_CODE", "open")


def test_signup_then_the_cookie_works(live, signup_open):
    client = server.app.test_client()
    r = client.post("/api/signup",
                    json={"email": "new@example.com", "password": "long-enough-pw"})
    assert r.status_code == 200
    assert client.get("/api/me").get_json()["user"]["email"] == "new@example.com"


def test_signup_rejects_a_short_password(live, signup_open):
    r = server.app.test_client().post(
        "/api/signup", json={"email": "x@example.com", "password": "short"})
    assert r.status_code == 400
    assert live.db.user_by_email("x@example.com") is None


def test_signup_rejects_a_duplicate_email(live, signup_open):
    r = server.app.test_client().post(
        "/api/signup", json={"email": "dm@example.com", "password": "long-enough-pw"})
    assert r.status_code == 400


def test_a_wrong_password_and_a_missing_account_look_the_same(live):
    """Different messages would turn the login form into a way of asking which
    email addresses are registered."""
    client = server.app.test_client()
    wrong = client.post("/api/login",
                        json={"email": "dm@example.com", "password": "not-the-password"})
    missing = client.post("/api/login",
                          json={"email": "nobody@example.com", "password": "not-the-password"})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.get_json() == missing.get_json()


def test_logout_forgets_the_dm_but_not_the_players(live):
    player = live.player_client("Rowan")
    player.post("/api/logout", json={})
    # The player never had a user cookie; their membership cookie survives, so
    # signing out of an account does not evict the tablet on the table.
    assert player.get(live.url("/config")).status_code == 200


# ---------------------------------------------------------------- gates ----

def test_a_stranger_cannot_read_a_table(live):
    stranger = server.app.test_client()
    assert stranger.get(live.url("/config")).status_code == 401
    assert stranger.get(live.url("/cast")).status_code == 401
    assert stranger.post(live.url("/ask"), json={"question": "hello"}).status_code == 401


def test_an_unknown_table_is_a_404_not_a_403(live):
    """Whether a table exists is not something a stranger gets to probe."""
    assert live.client.get("/api/t/no-such-table/config").status_code == 404


def test_a_member_of_one_table_cannot_open_another(live):
    other_user = live.db.create_user("other@example.com", "another-long-password")
    other_id = live.db.create_table(other_user, "Blackmoor", "blackmoor")

    assert live.client.get("/api/t/blackmoor/config").status_code == 401
    assert live.db.table(other_id) is not None   # it exists; they just may not open it


def test_a_membership_cookie_does_not_travel_between_tables(live):
    """The member id is checked against the table, not just looked up. A cookie
    minted at one table must not be usable at another even by a real member."""
    other_user = live.db.create_user("other@example.com", "another-long-password")
    other_id = live.db.create_table(other_user, "Blackmoor", "blackmoor")

    player = live.player_client("Rowan")
    stolen = player.get_cookie(member_cookie(live.row["id"]))
    player.set_cookie(member_cookie(other_id), stolen.value)

    assert player.get("/api/t/blackmoor/config").status_code == 401


def test_a_forged_cookie_is_refused(live):
    forger = server.app.test_client()
    forger.set_cookie(member_cookie(live.row["id"]), "not-a-signed-value")
    assert forger.get(live.url("/config")).status_code == 401


def test_a_rotated_invite_stops_the_old_link_working(live):
    old = live.db.table(live.row["id"])["invite_token"]
    live.db.rotate_invite(live.row["id"])
    r = server.app.test_client().post(f"/api/join/{old}", json={"name": "Late"})
    assert r.status_code == 404


def test_players_who_already_joined_keep_their_seat_after_a_rotation(live):
    """Rotating the link un-invites people who have not used it yet. It must not
    throw out the players already at the table mid-session."""
    player = live.player_client("Rowan")
    live.db.rotate_invite(live.row["id"])
    assert player.get(live.url("/config")).status_code == 200


# ----------------------------------------------------------------- roles ----

@pytest.mark.parametrize("method,path,body", [
    ("get", "/invite", None),
    ("post", "/invite/rotate", {}),
    ("get", "/usage", None),
    ("post", "/sessions/2026-08-24/reprocess", {}),
])
def test_dm_only_routes_refuse_a_player(live, method, path, body):
    """What is genuinely the DM's: the campaign, who is invited, and the bill.

    Notably NOT here: the familiar's persona, voice, and which one is bound.
    Those belong to whoever summoned it -- see test_familiar_ownership.py.
    """
    player = live.player_client()
    call = getattr(player, method)
    r = call(live.url(path), json=body) if body is not None else call(live.url(path))
    assert r.status_code == 403, f"{path} let a player through"


def test_a_player_may_still_ask_and_amend(live):
    """Players are not second-class: the familiar is for them. Only the table's
    configuration is the DM's."""
    player = live.player_client()
    assert player.get(live.url("/config")).status_code == 200
    assert player.get(live.url("/cast")).status_code == 200
    assert player.get(live.url("/sessions")).status_code == 200


def test_the_amender_is_the_signed_in_member(live):
    """`by` used to come from the request body, filled in from a name typed
    into localStorage. On a permanent record that is the client choosing its own
    attribution."""
    day = "2026-08-24"
    live.table.sessions_dir.mkdir(parents=True, exist_ok=True)
    (live.table.sessions_dir / f"{day}-timeline.log").write_text(
        '{"t": "2026-08-24T19:00:00+00:00", "id": "2026-08-24-0000", '
        '"event": "The party crossed."}\n', encoding="utf-8")

    player = live.player_client("Rowan")
    r = player.post(live.url(f"/sessions/{day}/timeline/{day}-0000/amend"),
                    json={"text": "It was a ford, not a bridge.", "by": "Someone Else"})
    assert r.status_code == 200
    assert r.get_json()["amendments"][0]["by"] == "Rowan"


# ------------------------------------------------------------------ CSRF ----

def test_a_form_post_is_refused(live):
    """Cookies plus a JSON API is the shape CSRF exploits. A cross-site form can
    only send form encodings; requiring application/json forces a preflight the
    attacker cannot satisfy. SameSite=Lax is the other half."""
    r = live.client.post(live.url("/forget"),
                         data="question=hello",
                         content_type="application/x-www-form-urlencoded")
    assert r.status_code == 415


def test_the_session_cookies_are_samesite_lax(live):
    client = server.app.test_client()
    r = client.post("/api/login",
                    json={"email": "dm@example.com", "password": "a-long-enough-password"})
    cookie = r.headers["Set-Cookie"]
    assert "SameSite=Lax" in cookie
    assert "HttpOnly" in cookie


# -------------------------------------------------------------- body bus ----

def test_the_body_bus_refuses_a_stranger(live):
    """Before Phase 1 the bus was covered by the site-wide basic_auth. With that
    gone, an unauthenticated socket would let anyone broadcast state to every
    body at every table."""
    with server.app.test_request_context("/"):
        assert server.ws_seat(live.row["slug"]) is None


def test_the_body_bus_admits_a_member(live):
    player = live.player_client("Rowan")
    cookie = player.get_cookie(member_cookie(live.row["id"]))
    with server.app.test_request_context(
            "/", headers={"Cookie": f"{cookie.key}={cookie.value}"}):
        seat = server.ws_seat(live.row["slug"])
    assert seat is not None and seat.rt.table_id == live.row["id"]
    # A browser drives the bus; a puppet only listens (see test_pairing.py).
    assert seat.may_drive and seat.device is None


def test_the_body_bus_refuses_an_unknown_table(live):
    with server.app.test_request_context("/"):
        assert server.ws_seat("no-such-table") is None
