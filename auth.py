"""Who is asking, and may they.

Two kinds of person use a table, and they arrive differently:

* The **DM** signed up with an email and a password. They get a `fam_user`
  cookie naming their user id, which is good at every table they own.
* A **player** followed an invite link and typed a display name. They never had
  an account. They get a per-table `fam_m_<table id>` cookie naming their member
  row -- enough to attribute an amendment to "Rowan", which is all the identity
  a post-game correction needs.

Both cookies are signed with itsdangerous, so the browser can hold them but not
forge them. Nothing here is a session store: there is no server-side session
table to keep, and logging out is deleting a cookie.

This module also carries the CSRF story, because Phase 1 removes the basic_auth
in front of the whole site and cookie-authenticated JSON endpoints are exactly
the shape CSRF exploits. Two overlapping defences, neither sufficient alone:
`SameSite=Lax` (a cross-site POST carries no cookie) and `require_json` (a
cross-site form cannot set Content-Type: application/json without a preflight
the browser will refuse).
"""

import functools
import os

from urllib.parse import quote

from flask import g, jsonify, redirect, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

# 30 days: long enough that a player who joined before a session is still
# recognised at the next one, short enough that a lost tablet stops working.
COOKIE_MAX_AGE = 60 * 60 * 24 * 30
USER_COOKIE = "fam_user"


def member_cookie(table_id: str) -> str:
    return f"fam_m_{table_id}"


def _secret() -> str:
    """The cookie signing key.

    A generated fallback is deliberately *not* provided: one would make every
    restart silently log everybody out, and the resulting "why do I keep having
    to sign in" bug is much harder to find than this exception.
    """
    key = os.environ.get("SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "SECRET_KEY is not set. Add a long random value to .env -- "
            "python -c 'import secrets; print(secrets.token_hex(32))'"
        )
    return key


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt=salt)


def sign(value: str, salt: str) -> str:
    return _serializer(salt).dumps(value)


def unsign(token: str, salt: str, max_age: int = COOKIE_MAX_AGE):
    if not token:
        return None
    try:
        return _serializer(salt).loads(token, max_age=max_age)
    except (BadSignature, Exception):
        return None


def set_cookie(response, name: str, value: str, salt: str):
    """Attach a signed cookie. Secure is on unless we are plainly on http --
    the LAN dev server and the iPad both run without TLS, and a Secure cookie
    there would simply never be sent."""
    response.set_cookie(
        name,
        sign(value, salt),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure,
        path="/",
    )
    return response


def clear_cookie(response, name: str):
    response.set_cookie(name, "", max_age=0, path="/")
    return response


# -- reading the request -----------------------------------------------------

def current_user(db):
    """The signed-in DM, or None. Cached on `g` for the request."""
    if "auth_user" not in g:
        uid = unsign(request.cookies.get(USER_COOKIE, ""), "user")
        g.auth_user = db.user(uid) if uid else None
    return g.auth_user


def resolve_member(db, table_row):
    """This request's membership of `table_row`, or None.

    The DM's user cookie is checked first so a DM who also holds a stale player
    cookie for their own table is still recognised as the DM.
    """
    user = current_user(db)
    if user is not None:
        member = db.member_for_user(table_row["id"], user["id"])
        if member is not None:
            return member
    token = request.cookies.get(member_cookie(table_row["id"]), "")
    mid = unsign(token, "member")
    if not mid:
        return None
    # Verified against the table, not just by id: a cookie minted at one table
    # must not open another.
    return db.member_in_table(table_row["id"], mid)


def wants_html() -> bool:
    """A browser navigating should be redirected to the login page; an API call
    should get JSON. Distinguish by what the caller says it accepts."""
    if request.path.startswith("/api/"):
        return False
    accept = request.headers.get("Accept", "")
    return "text/html" in accept or accept in ("", "*/*")


def _deny(status: int, message: str):
    """A browser gets sent somewhere it can act; an API call gets JSON.

    Only 401 redirects. A 403 means we know who you are and the answer is still
    no, so bouncing you to a login form would just invite you to sign in again
    and get the same refusal.
    """
    if status == 401 and wants_html():
        return redirect(f"/login.html?next={quote(request.full_path.rstrip('?'), safe='/')}")
    return jsonify({"error": message}), status


def require_json(view):
    """Reject state-changing requests that are not genuine JSON calls.

    This is the CSRF guard. A cross-origin <form> can only send
    application/x-www-form-urlencoded, multipart/form-data or text/plain; asking
    for application/json forces a preflight, which a cross-site attacker cannot
    satisfy. GET is exempt because a GET must not change anything anyway.
    """
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            ctype = (request.content_type or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return jsonify({"error": "expected Content-Type: application/json"}), 415
        return view(*args, **kwargs)
    return wrapped


def make_require_member(app, get_runtime):
    """Build the `@require_member` decorator for one Flask app.

    The database and the two roots are read from `app.config` on every request
    rather than captured when the decorator is built. That is a deliberate
    choice: the decorators are applied at import time, so a closure over them
    would freeze whatever configuration existed when server.py was first
    imported -- which a test suite (or anything that wants a second app) can
    never then change.
    """
    from tenancy import Table

    def require_member(role: str = "member"):
        if role not in ("member", "dm"):
            raise ValueError(f"unknown role requirement {role!r}")

        def decorator(view):
            @functools.wraps(view)
            def wrapped(slug, *args, **kwargs):
                db = app.config["DB"]
                data_root = app.config["DATA_DIR"]
                app_root = app.config["APP_ROOT"]
                row = db.table_by_slug(slug)
                if row is None:
                    # 404, not 403: whether a table exists is not something an
                    # unauthenticated stranger should be able to probe.
                    return _deny(404, "no such table")
                member = resolve_member(db, row)
                if member is None:
                    # No hint about invitations here: a stranger without a
                    # token cannot join anyway, and the DM's way in is the
                    # login form, which returns them to this page afterwards.
                    return _deny(401, "sign in first")
                if role == "dm" and member["role"] != "dm":
                    return _deny(403, "only the DM can do that")
                g.table = Table(row, data_root, app_root)
                g.member = member
                g.rt = get_runtime(row["id"])
                return view(*args, **kwargs)
            return wrapped
        return decorator

    return require_member
