"""Phase 6: pairing a puppet to a table.

The thing being tested is a credential handed to an object that lives in
somebody's house. So the tests are mostly about the edges of that: a code is
single-use and short-lived, a token belongs to exactly one table, a device can
be forgotten one at a time, and a body that is not a browser may listen to the
bus without being able to drive it.
"""

import db as database
import server


def _mint(live, name="Whiskers' puppet"):
    response = live.client.post(live.url("/devices"), json={"name": name})
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _claim(code, client=None):
    client = client or server.app.test_client()
    return client.post("/api/pair", json={"code": code})


# -- minting -----------------------------------------------------------------


def test_the_dm_mints_a_code_a_human_can_read_out(live):
    minted = _mint(live)
    assert minted["code"][4] == "-"                       # WXYZ-2345
    bare = database.normalize_pair_code(minted["code"])
    assert len(bare) == database.PAIR_CODE_LEN
    # No 0/O, 1/I/L or U: this is transcribed by hand from a screen to an SSH
    # session, usually by someone who is also running a game.
    assert set(bare) <= set(database.PAIR_ALPHABET)
    assert minted["minutes"] == database.PAIR_TTL_SECONDS // 60


def test_a_player_cannot_mint_one(live):
    player = live.player_client("Rowan")
    assert player.post(live.url("/devices"), json={}).status_code == 403
    assert player.get(live.url("/devices")).status_code == 403


def test_a_stranger_cannot_mint_one(live):
    stranger = server.app.test_client()
    assert stranger.post(live.url("/devices"), json={}).status_code == 401


def test_a_pending_device_is_listed_with_its_code(live):
    minted = _mint(live)
    listed = live.client.get(live.url("/devices")).get_json()["devices"]
    assert len(listed) == 1
    assert listed[0]["paired"] is False
    assert listed[0]["code"] == minted["code"]


def test_a_table_cannot_collect_unlimited_bodies(live):
    for _ in range(server.MAX_DEVICES_PER_TABLE):
        _mint(live)
    refused = live.client.post(live.url("/devices"), json={})
    assert refused.status_code == 409


def test_abandoned_codes_do_not_lock_the_table_out_forever(live):
    """Mint the cap's worth of codes during a fumbled SSH session, walk away,
    come back tomorrow. Counting before sweeping made that a table that could
    never pair a puppet again."""
    ids = [_mint(live)["id"] for _ in range(server.MAX_DEVICES_PER_TABLE)]
    con = live.db.connect()
    with con:
        con.executemany("UPDATE devices SET pair_expires = ? WHERE id = ?",
                        [("2020-01-01T00:00:00+00:00", i) for i in ids])

    assert live.client.post(live.url("/devices"), json={}).status_code == 201
    assert len(live.client.get(live.url("/devices")).get_json()["devices"]) == 1


# -- claiming ----------------------------------------------------------------


def test_the_puppet_claims_the_code_and_learns_where_to_connect(live):
    minted = _mint(live)
    body = _claim(minted["code"]).get_json()

    assert body["token"]
    assert body["table"]["slug"] == live.row["slug"]
    assert body["wsPath"] == f"/t/{live.row['slug']}/ws/state"
    assert body["device"]["name"] == "Whiskers' puppet"


def test_the_code_survives_being_typed_by_a_human(live):
    """Lowercase, spaces instead of the dash -- it was read off a screen."""
    minted = _mint(live)
    mangled = minted["code"].lower().replace("-", " ")
    assert _claim(mangled).status_code == 200


def test_a_code_works_exactly_once(live):
    minted = _mint(live)
    assert _claim(minted["code"]).status_code == 200
    second = _claim(minted["code"])
    assert second.status_code == 404
    assert "single-use" in second.get_json()["error"]


def test_an_expired_code_is_refused_and_swept(live):
    minted = _mint(live)
    con = live.db.connect()
    with con:
        con.execute("UPDATE devices SET pair_expires = ? WHERE id = ?",
                    ("2020-01-01T00:00:00+00:00", minted["id"]))

    assert _claim(minted["code"]).status_code == 404
    # And it does not sit in the DM's list forever as a code that looks live.
    assert live.client.get(live.url("/devices")).get_json()["devices"] == []


def test_nonsense_is_refused_without_touching_anything(live):
    _mint(live)
    for junk in ["", "   ", "not-a-code", "AAAA-AAAA", "'; DROP TABLE devices;--"]:
        assert _claim(junk).status_code == 404
    assert len(live.client.get(live.url("/devices")).get_json()["devices"]) == 1


def test_the_token_is_never_stored_in_the_clear(live):
    minted = _mint(live)
    token = _claim(minted["code"]).get_json()["token"]
    row = live.db.device_in_table(live.row["id"], minted["id"])
    assert row["token_hash"] and token not in row["token_hash"]
    assert row["pair_code"] is None and row["pair_expires"] is None
    assert live.db.device_by_token(token)["id"] == minted["id"]


def test_the_dm_never_sees_the_token_afterwards(live):
    minted = _mint(live)
    token = _claim(minted["code"]).get_json()["token"]
    listing = live.client.get(live.url("/devices")).get_data(as_text=True)
    assert token not in listing
    assert "token" not in listing


def test_a_paired_device_shows_as_paired_with_no_code(live):
    minted = _mint(live)
    _claim(minted["code"])
    device = live.client.get(live.url("/devices")).get_json()["devices"][0]
    assert device["paired"] is True
    assert device["code"] is None
    assert device["pairedAt"]


# -- holding the bus ---------------------------------------------------------


def _seat(slug, token):
    with server.app.test_request_context(
            "/", headers={"Authorization": f"Bearer {token}"}):
        return server.ws_seat(slug)


def test_a_paired_puppet_may_hold_the_bus(live):
    minted = _mint(live)
    token = _claim(minted["code"]).get_json()["token"]

    seat = _seat(live.row["slug"], token)
    assert seat is not None
    assert seat.rt.table_id == live.row["id"]
    assert seat.device["id"] == minted["id"]


def test_a_puppet_listens_but_does_not_drive(live):
    """A device token lives in a file inside an object somebody could pick up.
    The worst that should buy is watching the cat move."""
    minted = _mint(live)
    token = _claim(minted["code"]).get_json()["token"]
    assert _seat(live.row["slug"], token).may_drive is False


def test_connecting_records_that_the_puppet_was_seen(live):
    minted = _mint(live)
    token = _claim(minted["code"]).get_json()["token"]
    _seat(live.row["slug"], token)
    assert live.db.device_in_table(live.row["id"], minted["id"])["last_seen_at"]


def test_a_token_from_one_table_cannot_open_another(live):
    """The tenancy boundary, checked where a route cannot forget it."""
    other_id = live.db.create_table(live.user_id, "Other", "other")
    minted = _mint(live)
    token = _claim(minted["code"]).get_json()["token"]

    assert _seat(live.row["slug"], token) is not None
    assert _seat(live.db.table(other_id)["slug"], token) is None


def test_a_made_up_token_is_refused(live):
    assert _seat(live.row["slug"], "not-a-real-token") is None
    assert _seat(live.row["slug"], "") is None


def test_only_a_bearer_header_counts(live):
    """Basic auth was the old shared-password way in. It is not a device."""
    minted = _mint(live)
    token = _claim(minted["code"]).get_json()["token"]
    with server.app.test_request_context(
            "/", headers={"Authorization": f"Basic {token}"}):
        assert server.ws_seat(live.row["slug"]) is None


# -- forgetting --------------------------------------------------------------


def test_forgetting_a_device_kills_its_token(live):
    minted = _mint(live)
    token = _claim(minted["code"]).get_json()["token"]
    assert _seat(live.row["slug"], token) is not None

    assert live.client.delete(live.url(f"/devices/{minted['id']}"),
                              json={}).status_code == 200
    assert _seat(live.row["slug"], token) is None
    assert live.client.get(live.url("/devices")).get_json()["devices"] == []


def test_forgetting_one_device_leaves_the_others_alone(live):
    first, second = _mint(live, "First"), _mint(live, "Second")
    first_token = _claim(first["code"]).get_json()["token"]
    second_token = _claim(second["code"]).get_json()["token"]

    live.client.delete(live.url(f"/devices/{first['id']}"), json={})
    assert _seat(live.row["slug"], first_token) is None
    assert _seat(live.row["slug"], second_token) is not None


def test_a_player_cannot_forget_the_puppet(live):
    minted = _mint(live)
    player = live.player_client("Rowan")
    assert player.delete(live.url(f"/devices/{minted['id']}"),
                         json={}).status_code == 403


def test_forgetting_something_that_is_not_there(live):
    assert live.client.delete(live.url("/devices/nope"), json={}).status_code == 404


def test_deleting_a_table_takes_its_devices_with_it(live):
    """Devices cascade from the table row. A token outliving the table it was
    minted at would be a credential pointing at nothing -- or, after an id was
    reused, at something else."""
    minted = _mint(live)
    token = _claim(minted["code"]).get_json()["token"]
    live.db.delete_table(live.row["id"])
    assert live.db.device_by_token(token) is None
