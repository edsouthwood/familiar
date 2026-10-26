"""Limits on what a request may carry, and the cookie flag that depends on the proxy.

Both exist because the site is reachable from the internet with only the app's
own accounts in front of it: any member can POST, and every byte of a question
or an ambient chunk is a byte some provider bills for.
"""

import server


def test_cookies_are_secure_behind_the_tls_proxy(live):
    """Caddy terminates TLS and forwards plain http. Without ProxyFix the app
    thought every request was insecure and never set Secure."""
    client = server.app.test_client()
    response = client.post(
        "/api/login",
        json={"email": "dm@example.com", "password": "a-long-enough-password"},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 200
    assert "Secure" in response.headers["Set-Cookie"]


def test_plain_http_still_gets_a_usable_cookie(live):
    """The LAN dev server has no TLS; a Secure cookie there is never sent back."""
    client = server.app.test_client()
    response = client.post(
        "/api/login",
        json={"email": "dm@example.com", "password": "a-long-enough-password"},
    )
    assert "Secure" not in response.headers["Set-Cookie"]


def test_an_oversized_body_is_refused_before_the_view(live):
    body = '{"question": "' + "a" * (2 * 1024 * 1024) + '"}'
    response = live.client.post(live.url("/ask"), data=body,
                                content_type="application/json")
    assert response.status_code == 413


def test_an_overlong_question_never_reaches_the_model(live, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the model was called")
    monkeypatch.setattr(server, "ask_llm", boom)
    monkeypatch.setattr(server, "ask_llm_stream", boom)
    question = "a" * (server.MAX_QUESTION_CHARS + 1)
    for route in ("/ask", "/ask-stream"):
        response = live.client.post(live.url(route), json={"question": question})
        assert response.status_code == 413


def test_overlong_tts_text_is_refused(live, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the voice was called")
    monkeypatch.setattr(server, "synthesize_speech", boom)
    response = live.client.post(live.url("/tts"),
                                json={"text": "a" * (server.MAX_TTS_CHARS + 1)})
    assert response.status_code == 413


def test_an_overlong_ambient_chunk_is_dropped_quietly(live):
    """The browser does not await /ambient, so the drop is silent -- but the
    text must not land in the buffer that feeds the next fold-in."""
    response = live.client.post(live.url("/ambient"),
                                json={"text": "word " * server.MAX_AMBIENT_CHARS})
    assert response.status_code == 200
    assert live.rt.ambient_buffer == ""


# -- the doors that take no cookie --------------------------------------------

import ratelimit  # noqa: E402

BAD_LOGIN = {"email": "dm@example.com", "password": "not-the-password"}
GOOD_LOGIN = {"email": "dm@example.com", "password": "a-long-enough-password"}


def _from(ip):
    """Headers as Caddy would send them for a client at `ip`."""
    return {"X-Forwarded-For": ip}


def test_a_password_guesser_is_slowed_after_ten_misses(live):
    client = server.app.test_client()
    for _ in range(ratelimit.LOGIN_FAILURES_PER_IP.limit):
        assert client.post("/api/login", json=BAD_LOGIN).status_code == 401
    response = client.post("/api/login", json=BAD_LOGIN)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    # Refused before the password is checked: the right one does not get in
    # either, or the limit would be an oracle for a correct guess.
    assert client.post("/api/login", json=GOOD_LOGIN).status_code == 429


def test_signing_in_successfully_never_counts(live):
    client = server.app.test_client()
    for _ in range(ratelimit.LOGIN_FAILURES_PER_IP.limit + 5):
        assert client.post("/api/login", json=GOOD_LOGIN).status_code == 200


def test_the_limit_is_per_address_behind_the_proxy(live):
    """Without ProxyFix's x_for every request is from 127.0.0.1, and one
    guesser would lock the DM out along with themself."""
    client = server.app.test_client()
    for _ in range(ratelimit.LOGIN_FAILURES_PER_IP.limit):
        client.post("/api/login", json={**BAD_LOGIN, "email": "x@example.com"},
                    headers=_from("203.0.113.9"))
    assert client.post("/api/login", json=GOOD_LOGIN,
                       headers=_from("198.51.100.4")).status_code == 200


def test_one_account_is_protected_across_many_addresses(live):
    client = server.app.test_client()
    for i in range(ratelimit.LOGIN_FAILURES_PER_EMAIL.limit):
        client.post("/api/login", json=BAD_LOGIN, headers=_from(f"203.0.113.{i}"))
    response = client.post("/api/login", json=BAD_LOGIN, headers=_from("198.51.100.4"))
    assert response.status_code == 429


def test_signup_attempts_are_capped_even_when_they_succeed(live, monkeypatch):
    monkeypatch.setenv("SIGNUP_CODE", "open")
    client = server.app.test_client()
    for i in range(ratelimit.SIGNUP_ATTEMPTS_PER_IP.limit):
        r = client.post("/api/signup", json={"email": f"n{i}@example.com",
                                             "password": "a-long-enough-password"})
        assert r.status_code == 200
    r = client.post("/api/signup", json={"email": "one-more@example.com",
                                         "password": "a-long-enough-password"})
    assert r.status_code == 429
    assert live.db.user_by_email("one-more@example.com") is None


def test_pairing_codes_cannot_be_sprayed(live):
    client = server.app.test_client()
    for _ in range(ratelimit.PAIR_FAILURES_PER_IP.limit):
        assert client.post("/api/pair", json={"code": "WRONG-CODE"}).status_code == 404
    assert client.post("/api/pair", json={"code": "WRONG-CODE"}).status_code == 429


def test_invite_tokens_cannot_be_sprayed_by_either_door(live):
    client = server.app.test_client()
    for i in range(ratelimit.JOIN_FAILURES_PER_IP.limit):
        door = f"/join/nope{i}" if i % 2 else f"/api/join/nope{i}"
        if door.startswith("/api"):
            client.post(door, json={"name": "Mallory"})
        else:
            client.get(door)
    token = live.db.table(live.table.id)["invite_token"]
    assert client.post(f"/api/join/{token}", json={"name": "Mallory"}).status_code == 429
    assert client.get(f"/join/{token}").status_code == 429
