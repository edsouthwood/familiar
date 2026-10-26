"""Who may create an account.

The gate exists because of a specific hazard: the deployment notes tell an
operator to delete the reverse proxy's shared password once the app's own
accounts work, and doing that used to open account creation to the internet —
where every table a stranger made would spend the operator's LLM budget. So the
state you get by *forgetting to configure anything* is the safe one, and the
tests that matter most here are the ones about defaults.
"""

import pytest

import server


def _signup(client, email="new@example.com", password="a-long-enough-password", **extra):
    return client.post("/api/signup",
                       json={"email": email, "password": password, **extra})


@pytest.fixture
def stranger():
    return server.app.test_client()


# -- the default, which is the whole point -----------------------------------


def test_a_deployment_with_users_and_no_code_takes_nobody_new(live, stranger, monkeypatch):
    """The state you get by forgetting. Previously this was wide open."""
    monkeypatch.delenv("SIGNUP_CODE", raising=False)
    response = _signup(stranger)
    assert response.status_code == 403
    assert "not taking new accounts" in response.get_json()["error"]
    assert live.db.user_by_email("new@example.com") is None


def test_the_very_first_account_needs_no_code(tmp_path, monkeypatch, stranger):
    """A fresh install has nobody who could have issued a code, so the first
    account is allowed — a window exactly one account wide."""
    import db as database
    monkeypatch.delenv("SIGNUP_CODE", raising=False)
    monkeypatch.setenv("SECRET_KEY", "test-key-not-a-secret")
    empty = database.Database(tmp_path / "fresh.db")
    monkeypatch.setattr(server, "db", empty)
    monkeypatch.setitem(server.app.config, "DB", empty)

    assert server.signup_mode() == "open"
    assert _signup(stranger, email="first@example.com").status_code == 200
    # ...and it closes behind them.
    assert server.signup_mode() == "closed"
    assert _signup(stranger, email="second@example.com").status_code == 403


# -- with a code configured --------------------------------------------------


def test_the_right_code_gets_you_in(live, stranger, monkeypatch):
    monkeypatch.setenv("SIGNUP_CODE", "dragons-2026")
    assert _signup(stranger, code="dragons-2026").status_code == 200
    assert live.db.user_by_email("new@example.com") is not None


def test_a_wrong_or_missing_code_does_not(live, stranger, monkeypatch):
    monkeypatch.setenv("SIGNUP_CODE", "dragons-2026")
    for attempt in ({}, {"code": ""}, {"code": "wrong"}, {"code": "dragons-2027"}):
        response = _signup(stranger, **attempt)
        assert response.status_code == 403
        # One message for wrong and for missing: the same fact from outside.
        assert response.get_json()["error"] == "that invitation code is not right"
    assert live.db.user_by_email("new@example.com") is None


def test_the_code_survives_being_typed_by_a_human(live, stranger, monkeypatch):
    """It gets read off a screen, said aloud, or pasted out of a message."""
    monkeypatch.setenv("SIGNUP_CODE", "Dragons-2026")
    assert _signup(stranger, code="  dragons-2026 ").status_code == 200


def test_open_means_open(live, stranger, monkeypatch):
    """A deliberate word, so that opening a service to the world is something
    somebody typed rather than something they omitted."""
    monkeypatch.setenv("SIGNUP_CODE", "open")
    assert server.signup_mode() == "open"
    assert _signup(stranger).status_code == 200


def test_the_code_is_never_disclosed(live, stranger, monkeypatch):
    monkeypatch.setenv("SIGNUP_CODE", "dragons-2026")
    body = stranger.get("/api/signup").get_data(as_text=True)
    assert "dragons" not in body.lower()
    assert stranger.get("/api/signup").get_json() == {"mode": "code"}
    refused = _signup(stranger, code="wrong").get_data(as_text=True)
    assert "dragons" not in refused.lower()


# -- the form's own question -------------------------------------------------


@pytest.mark.parametrize("configured,expected", [
    ("dragons-2026", "code"),
    ("open", "open"),
    ("OPEN", "open"),
    (None, "closed"),
])
def test_the_policy_endpoint_tells_the_form_what_to_draw(live, stranger, monkeypatch,
                                                         configured, expected):
    if configured is None:
        monkeypatch.delenv("SIGNUP_CODE", raising=False)
    else:
        monkeypatch.setenv("SIGNUP_CODE", configured)
    assert stranger.get("/api/signup").get_json()["mode"] == expected


def test_the_policy_is_readable_without_an_account(live, stranger, monkeypatch):
    """It is the signed-out signup page that asks."""
    monkeypatch.setenv("SIGNUP_CODE", "dragons-2026")
    assert stranger.get("/api/signup").status_code == 200


# -- what the gate must not touch --------------------------------------------


def test_the_gate_does_not_touch_signing_in(live, monkeypatch):
    """An existing DM must not be locked out of their own campaign by a setting
    about *new* accounts."""
    monkeypatch.delenv("SIGNUP_CODE", raising=False)
    client = server.app.test_client()
    assert client.post("/api/login", json={"email": "dm@example.com",
                                           "password": "a-long-enough-password"}
                       ).status_code == 200


def test_the_gate_does_not_touch_players_joining(live, monkeypatch):
    """Players never sign up — they follow an invitation. Closing signup must
    not close the table."""
    monkeypatch.delenv("SIGNUP_CODE", raising=False)
    player = live.player_client("Rowan")
    assert player.get(live.url("/config")).status_code == 200


def test_signup_still_refuses_a_bad_email_after_the_gate(live, stranger, monkeypatch):
    """The gate is a new first hurdle, not a replacement for the old checks."""
    monkeypatch.setenv("SIGNUP_CODE", "open")
    assert _signup(stranger, email="not-an-email").status_code == 400
    assert _signup(stranger, password="short").status_code == 400
