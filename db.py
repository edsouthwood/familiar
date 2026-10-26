"""All SQL for the app lives here.

The tenancy model is a hybrid: one shared SQLite database holds the things that
are *about* tables -- who owns them, who may enter, what they have spent --
while everything a table *accumulates* stays as files under
data/tables/<id>/. Nothing in this module knows about the filesystem, and
nothing outside this module writes SQL. That is the whole discipline, and it is
what makes a later move to Postgres a rewrite of one file.

Note one deliberate departure from the original schema sketch: there is no
`familiars` table. Familiars are directories (see familiars.py) because a
familiar is mostly prose -- a persona and a bond -- and prose belongs in files
next to the campaign notes it grew out of. Putting the name and wake words in
SQL while the persona sat in a file would have given the same object two homes.
"""

import hashlib
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- A table is a tenant: one campaign, one group of players, one data directory.
-- `id` names the directory and never changes; `slug` is what appears in URLs
-- and may be renamed, so the two are kept apart on purpose.
CREATE TABLE IF NOT EXISTS tables (
    id           TEXT PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name         TEXT NOT NULL,
    owner_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    invite_token TEXT NOT NULL UNIQUE,
    plan         TEXT NOT NULL DEFAULT 'base',
    created_at   TEXT NOT NULL
);

-- Membership is the join between a person and a table. The DM has a user_id
-- (they signed up with an email); players usually do not -- they arrived
-- through an invite link and typed a display name, which is exactly the
-- identity the amendment log needs and no more.
CREATE TABLE IF NOT EXISTS members (
    id           TEXT PRIMARY KEY,
    table_id     TEXT NOT NULL REFERENCES tables(id) ON DELETE CASCADE,
    user_id      TEXT REFERENCES users(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('dm', 'player')),
    created_at   TEXT NOT NULL
);

-- What each table has spent, one row per (table, month, provider).
--
-- Aggregated on write rather than stored as a log of individual calls: the
-- question this table exists to answer is "what does a table cost per month",
-- and keeping a row per request would mean millions of rows to answer it. The
-- cache columns are here because they are the whole point -- a cached input
-- token costs about a tenth of a fresh one, so a bill cannot be predicted from
-- input_tokens alone.
CREATE TABLE IF NOT EXISTS usage (
    table_id           TEXT    NOT NULL REFERENCES tables(id) ON DELETE CASCADE,
    month              TEXT    NOT NULL,          -- 'YYYY-MM', UTC
    provider           TEXT    NOT NULL,
    requests           INTEGER NOT NULL DEFAULT 0,
    llm_tokens_in      INTEGER NOT NULL DEFAULT 0,
    llm_tokens_out     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    tts_chars          INTEGER NOT NULL DEFAULT 0,
    tts_ms             INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (table_id, month, provider)
);

-- A body that is not a browser: the puppet on the table, or anything else
-- that holds the state bus without a person sitting in front of it.
--
-- Why a row at all, when a browser needs none: a browser has a member cookie
-- because a human signed in or followed an invitation, and it can be asked to
-- do that again. A cat on a shelf cannot follow a link, and the credential it
-- holds sits in a file inside a physical object that lives in somebody's
-- house. So it gets an identity that can be listed, named, and revoked one at
-- a time, without changing anything for the people at the table.
--
-- One row covers the whole life of a device, pending and paired:
--
--   minted    pair_code set, pair_expires set, token_hash NULL
--   claimed   pair_code NULL, pair_expires NULL, token_hash set
--
-- `token_hash` rather than the token: this is the one credential in the
-- service that is written down inside a device someone could pick up, and a
-- stolen database should not also hand over every puppet.
CREATE TABLE IF NOT EXISTS devices (
    id           TEXT PRIMARY KEY,
    table_id     TEXT NOT NULL REFERENCES tables(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    token_hash   TEXT UNIQUE,          -- NULL until the code is claimed
    pair_code    TEXT UNIQUE,          -- NULL once it has been
    pair_expires TEXT,                 -- NULL once it has been
    created_at   TEXT NOT NULL,
    paired_at    TEXT,
    last_seen_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS members_table_user
    ON members(table_id, user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS devices_by_table ON devices(table_id);
CREATE INDEX IF NOT EXISTS members_by_table ON members(table_id);
CREATE INDEX IF NOT EXISTS tables_by_owner  ON tables(owner_id);

-- Full-text search over everything a table has recorded. This is an INDEX,
-- never a source of truth: every row in it is derived from a file under
-- data/tables/<id>/, and tools/reindex_search.py rebuilds the whole thing from
-- those files. That is what lets every write into it be best-effort -- a
-- failed insert costs a search miss until the next reindex, not a lost event.
--
-- Only `text` is indexed; the rest are stored for display and filtering.
-- Filtering by table_id in SQL rather than in Python is not an optimisation,
-- it is the tenancy boundary: this is the one table in the schema holding
-- every tenant's prose in one place.
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    text,
    table_id UNINDEXED,
    kind     UNINDEXED,   -- event | npc | place | qa
    ref      UNINDEXED,   -- how the client links back to it; unique within (table_id, kind)
    date     UNINDEXED,
    title    UNINDEXED
);
"""

# The kinds of thing the index holds. `event` covers a timeline event *and*
# every amendment to it in one document, rather than the separate `amendment`
# kind first sketched: an amended event should be one hit that finds the word
# whether the room said it or a player corrected it in afterwards, not two
# rows competing for the same rank.
SEARCH_KINDS = ("event", "npc", "place", "qa")

# Highlight markers. The API hands back a snippet with the matched span
# wrapped in these, and the client splits on them to build <mark> elements
# through the DOM. Deliberately control characters rather than HTML: this text
# was transcribed from a room by an LLM, and it must never reach innerHTML.
MARK_START = "\u0001"
MARK_END = "\u0002"

_FTS_TOKEN_RE = re.compile(r'[^\s"]+')


def fts_query(raw: str) -> str:
    """Turn whatever somebody typed into a MATCH expression that cannot error.

    FTS5's query language is a real syntax -- AND, OR, NOT, NEAR, column
    filters, parentheses, prefixes -- and feeding a search box straight into it
    means a stray quote or a lone hyphen raises OperationalError at the user.
    So every whitespace-separated run becomes a quoted phrase, which reduces
    the whole language to literals: no input can be a syntax error, and no
    input can reach across into another column filter.

    The cost is that a player cannot type `chapel OR crypt` and have it mean
    anything but those three words. That is the right trade for a search box
    nobody will read the manual for. The last token gets a `*` so typing
    "chap" finds "chapel" while you are still typing.

    Returns "" when there is nothing searchable, which callers must treat as
    "no results" rather than passing on to MATCH.
    """
    tokens = _FTS_TOKEN_RE.findall(raw or "")
    if not tokens:
        return ""
    phrases = ['"' + t.replace('"', "") + '"' for t in tokens]
    phrases[-1] += "*"
    return " ".join(phrases)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _later(seconds: int) -> str:
    """A timestamp `seconds` from now, in the same shape as _now().

    Same shape matters: expiry is compared as a string in SQL, which is only
    correct because every timestamp here is fixed-width UTC ISO with the same
    offset. A naive datetime, or a local one, would sort wrong at 01:00 in
    October and nowhere else.
    """
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds")


# -- pairing codes and device tokens ----------------------------------------
#
# Two secrets with opposite jobs, which is why they look nothing alike.
#
# The PAIRING CODE is read out across a table and typed into a Pi over SSH, so
# it is short and unambiguous: 8 characters from an alphabet with no 0/O, 1/I/L
# or U (which is heard as "you"). Its safety comes from being single-use and
# short-lived, not from length -- 30^8 is ~39 bits, which is far beyond guessing
# inside fifteen minutes, and it is gone the moment it is claimed.
#
# The DEVICE TOKEN is never read by a human. It is 32 random bytes, stored in a
# file on the Pi and hashed here.
PAIR_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
PAIR_CODE_LEN = 8
PAIR_TTL_SECONDS = 15 * 60


def new_pair_code() -> str:
    return "".join(secrets.choice(PAIR_ALPHABET) for _ in range(PAIR_CODE_LEN))


def format_pair_code(code: str) -> str:
    """WXYZ-2345: grouped for reading aloud. Only ever for display -- the
    stored form and the compared form are the bare characters."""
    return f"{code[:4]}-{code[4:]}"


def normalize_pair_code(raw: str) -> str:
    """What somebody typed, as the thing that was minted.

    Case and punctuation are noise here: the code is transcribed by hand from a
    screen to a terminal, and refusing "wxyz 2345" because of a space would be
    the service being right and useless.
    """
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


def hash_device_token(token: str) -> str:
    """SHA-256, deliberately, where passwords get werkzeug's slow hash.

    The difference is what the input is. A password is chosen by a human and
    guessable, so it needs a hash that is expensive per attempt. A device token
    is 256 bits from `secrets`, so there is nothing to guess and the only job
    left is that a leaked database does not contain usable credentials. A slow
    hash here would buy nothing and would be paid on every websocket connect.
    """
    return hashlib.sha256((token or "").encode()).hexdigest()


def new_id(nbytes: int = 4) -> str:
    """8 hex characters by default. Table ids appear in URLs, so they are short
    enough to say aloud and random enough that guessing one is pointless -- but
    they are not a security boundary; every route is auth-gated regardless."""
    return secrets.token_hex(nbytes)


class Database:
    """A SQLite database with one connection per thread.

    Per-thread connections rather than one shared connection: gunicorn runs
    threaded workers, and sqlite3 objects are not safe to hand between threads.
    WAL means a reader never blocks on the writer, which matters because the
    body-bus threads hold their connections open for the life of a websocket.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._local = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("PRAGMA busy_timeout = 5000")
            self._local.con = con
        return con

    def close(self) -> None:
        """Close this thread's connection. Only tests need this."""
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None

    # -- users ---------------------------------------------------------------

    def create_user(self, email: str, password: str) -> str:
        email = (email or "").strip()
        if not email or "@" not in email:
            raise ValueError("a real email address is required")
        if len(password or "") < 8:
            raise ValueError("password must be at least 8 characters")
        uid = new_id(8)
        con = self.connect()
        with con:
            try:
                con.execute(
                    "INSERT INTO users (id, email, password_hash, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (uid, email, generate_password_hash(password), _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("that email is already registered") from exc
        return uid

    def any_users(self) -> bool:
        """Whether this deployment has anybody at all.

        Exists for one question: whether the *first* account is being created.
        A brand-new install has to let somebody in without a code -- there is
        nobody to have issued one -- and after that it must not.
        """
        return self.connect().execute(
            "SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def user_by_email(self, email: str):
        return self.connect().execute(
            "SELECT * FROM users WHERE email = ?", ((email or "").strip(),)
        ).fetchone()

    def user(self, user_id: str):
        return self.connect().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()

    def check_login(self, email: str, password: str):
        """Return the user row on success, None on any failure.

        Deliberately one function with one return shape: callers that can tell
        "no such email" from "wrong password" leak which emails are registered.
        """
        row = self.user_by_email(email)
        if row is None:
            # Hash anyway so a missing account and a wrong password take
            # comparable time.
            generate_password_hash(password or "")
            return None
        if not check_password_hash(row["password_hash"], password or ""):
            return None
        return row

    def set_password(self, user_id: str, password: str) -> None:
        if len(password or "") < 8:
            raise ValueError("password must be at least 8 characters")
        con = self.connect()
        with con:
            con.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(password), user_id),
            )

    # -- tables --------------------------------------------------------------

    def create_table(self, owner_id: str, name: str, slug: str = "",
                     role: str = "dm") -> str:
        """Create a table owned by `owner_id`, with their membership.

        The owner is inserted as a member in the same transaction as the table.
        A table whose creator is not a member is a table nobody can open, and
        that is not a state worth being able to represent.

        `owner_id` is who *owns* the table -- the billing relationship. `role`
        is what they do at it. They default to the same person because usually
        the DM sets things up, but they are deliberately separable: unlike most
        tools of this kind, the buyer here may well be the player who summoned
        the familiar rather than the DM, and a schema that forced the payer to
        run the game would have to be unpicked to allow it.
        """
        if role not in ("dm", "player"):
            raise ValueError(f"unknown role {role!r}")
        name = (name or "").strip() or "Untitled table"
        tid = new_id()
        slug = (slug or "").strip().lower() or tid
        # The slug is a URL path segment -- /t/<slug>/ and every API route under
        # it. Flask's default converter refuses '/', so a slug carrying one (or
        # a space, '#', '?') would create a table no route can ever address,
        # including the delete endpoint: unreachable and undeletable, but still
        # counting against its owner. Checked here, in the one place tables are
        # made, rather than in each caller.
        if not re.fullmatch(r"[a-z0-9-]{1,64}", slug):
            raise ValueError(
                "the short name can only use letters, numbers and hyphens")
        owner = self.user(owner_id)
        if owner is None:
            raise ValueError("no such user")
        con = self.connect()
        with con:
            try:
                con.execute(
                    "INSERT INTO tables (id, slug, name, owner_id, invite_token,"
                    " plan, created_at) VALUES (?, ?, ?, ?, ?, 'base', ?)",
                    (tid, slug, name, owner_id, new_id(16), _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"the name {slug!r} is already taken") from exc
            con.execute(
                "INSERT INTO members (id, table_id, user_id, display_name, role,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (new_id(8), tid, owner_id, owner["email"].split("@")[0], role, _now()),
            )
        return tid

    def table(self, table_id: str):
        return self.connect().execute(
            "SELECT * FROM tables WHERE id = ?", (table_id,)
        ).fetchone()

    def table_by_slug(self, slug: str):
        return self.connect().execute(
            "SELECT * FROM tables WHERE slug = ?", ((slug or "").strip(),)
        ).fetchone()

    def table_by_invite(self, token: str):
        if not token:
            return None
        return self.connect().execute(
            "SELECT * FROM tables WHERE invite_token = ?", (token,)
        ).fetchone()

    def tables_for_user(self, user_id: str) -> list:
        return self.connect().execute(
            "SELECT t.*, m.role FROM tables t JOIN members m ON m.table_id = t.id"
            " WHERE m.user_id = ? ORDER BY t.created_at",
            (user_id,),
        ).fetchall()

    def all_tables(self) -> list:
        return self.connect().execute(
            "SELECT * FROM tables ORDER BY created_at"
        ).fetchall()

    def rename_table(self, table_id: str, name: str = "", slug: str = "") -> None:
        sets, args = [], []
        if name.strip():
            sets.append("name = ?")
            args.append(name.strip())
        if slug.strip():
            sets.append("slug = ?")
            args.append(slug.strip().lower())
        if not sets:
            return
        args.append(table_id)
        con = self.connect()
        with con:
            try:
                con.execute(f"UPDATE tables SET {', '.join(sets)} WHERE id = ?", args)
            except sqlite3.IntegrityError as exc:
                raise ValueError("that name is already taken") from exc

    def set_plan(self, table_id: str, plan: str) -> None:
        con = self.connect()
        with con:
            con.execute("UPDATE tables SET plan = ? WHERE id = ?", (plan, table_id))

    def rotate_invite(self, table_id: str) -> str:
        """Mint a new invite token, invalidating the old link."""
        token = new_id(16)
        con = self.connect()
        with con:
            con.execute(
                "UPDATE tables SET invite_token = ? WHERE id = ?", (token, table_id)
            )
        return token

    # -- members -------------------------------------------------------------

    def add_member(self, table_id: str, display_name: str, role: str = "player",
                   user_id: str | None = None) -> str:
        display_name = (display_name or "").strip()
        if not display_name:
            raise ValueError("a display name is required")
        if role not in ("dm", "player"):
            raise ValueError(f"unknown role {role!r}")
        mid = new_id(8)
        con = self.connect()
        with con:
            con.execute(
                "INSERT INTO members (id, table_id, user_id, display_name, role,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (mid, table_id, user_id, display_name, role, _now()),
            )
        return mid

    def member(self, member_id: str):
        return self.connect().execute(
            "SELECT * FROM members WHERE id = ?", (member_id,)
        ).fetchone()

    def member_in_table(self, table_id: str, member_id: str):
        """The lookup the request path actually needs: a member *and* proof they
        belong to this table. Checking the id alone would let a cookie minted at
        one table open another."""
        return self.connect().execute(
            "SELECT * FROM members WHERE id = ? AND table_id = ?",
            (member_id, table_id),
        ).fetchone()

    def member_for_user(self, table_id: str, user_id: str):
        return self.connect().execute(
            "SELECT * FROM members WHERE table_id = ? AND user_id = ?",
            (table_id, user_id),
        ).fetchone()

    def members(self, table_id: str) -> list:
        return self.connect().execute(
            "SELECT * FROM members WHERE table_id = ? ORDER BY created_at",
            (table_id,),
        ).fetchall()

    def remove_member(self, table_id: str, member_id: str) -> bool:
        """Remove a player. The DM is not removable -- a table with no DM cannot
        be administered, and deleting the table is the honest way to end it."""
        con = self.connect()
        with con:
            cur = con.execute(
                "DELETE FROM members WHERE id = ? AND table_id = ? AND role = 'player'",
                (member_id, table_id),
            )
        return cur.rowcount > 0

    def rename_member(self, table_id: str, member_id: str, display_name: str) -> None:
        display_name = (display_name or "").strip()
        if not display_name:
            raise ValueError("a display name is required")
        con = self.connect()
        with con:
            con.execute(
                "UPDATE members SET display_name = ? WHERE id = ? AND table_id = ?",
                (display_name, member_id, table_id),
            )


    # -- usage ---------------------------------------------------------------

    USAGE_FIELDS = ("requests", "llm_tokens_in", "llm_tokens_out",
                    "cache_read_tokens", "cache_write_tokens",
                    "tts_chars", "tts_ms")

    def record_usage(self, table_id: str, provider: str, month: str = "", **amounts) -> None:
        """Add to this table's running totals for the month. Never overwrites.

        Best-effort by contract: metering must not be able to break answering,
        so the caller swallows failures. Unknown keyword arguments are a
        programming error and do raise -- a silently ignored `tts_chars=` typo
        would show up months later as a table that appears to cost nothing.
        """
        unknown = set(amounts) - set(self.USAGE_FIELDS)
        if unknown:
            raise ValueError(f"unknown usage field(s): {sorted(unknown)}")
        amounts = {k: int(v) for k, v in amounts.items() if v}
        if not amounts:
            return
        month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        cols = list(amounts)
        placeholders = ", ".join("?" for _ in cols)
        # SQLite's upsert: insert the row, or add to the one already there.
        bumps = ", ".join(f"{c} = usage.{c} + excluded.{c}" for c in cols)
        con = self.connect()
        with con:
            con.execute(
                f"INSERT INTO usage (table_id, month, provider, {', '.join(cols)})"
                f" VALUES (?, ?, ?, {placeholders})"
                f" ON CONFLICT (table_id, month, provider) DO UPDATE SET {bumps}",
                [table_id, month, provider, *(amounts[c] for c in cols)],
            )

    def usage_for_month(self, table_id: str, month: str = "") -> list:
        month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        return self.connect().execute(
            "SELECT * FROM usage WHERE table_id = ? AND month = ? ORDER BY provider",
            (table_id, month),
        ).fetchall()

    def usage_totals(self, table_id: str, month: str = "") -> dict:
        """This month's totals across every provider."""
        month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        sums = ", ".join(f"COALESCE(SUM({f}), 0) AS {f}" for f in self.USAGE_FIELDS)
        row = self.connect().execute(
            f"SELECT {sums} FROM usage WHERE table_id = ? AND month = ?",
            (table_id, month),
        ).fetchone()
        return dict(row) if row else {f: 0 for f in self.USAGE_FIELDS}

    def usage_history(self, table_id: str, months: int = 12) -> list:
        sums = ", ".join(f"SUM({f}) AS {f}" for f in self.USAGE_FIELDS)
        return self.connect().execute(
            f"SELECT month, {sums} FROM usage WHERE table_id = ?"
            " GROUP BY month ORDER BY month DESC LIMIT ?",
            (table_id, months),
        ).fetchall()


    # -- devices (the puppet on the table) -----------------------------------
    #
    # Pairing is two steps on purpose. The DM mints a code in the browser they
    # are already signed into; the device redeems it once, from wherever it is,
    # and gets a token of its own. Nothing the DM types ever reaches the Pi and
    # nothing the Pi holds can be used to sign in as anybody.

    def mint_pair_code(self, table_id: str, name: str = "") -> dict:
        """Create a pending device and the short code that claims it.

        Returns the code in the clear -- this is the only moment it exists
        outside the DM's screen. Expired pending devices are swept first, so a
        DM who mints three codes while fumbling an SSH session does not
        accumulate three dead rows in their device list.
        """
        self.expire_pair_codes()
        name = (name or "").strip() or "Puppet"
        con = self.connect()
        for _ in range(5):        # a collision is ~impossible; a retry is free
            code = new_pair_code()
            device_id = new_id(8)
            try:
                with con:
                    con.execute(
                        "INSERT INTO devices (id, table_id, name, pair_code,"
                        " pair_expires, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (device_id, table_id, name, code,
                         _later(PAIR_TTL_SECONDS), _now()),
                    )
            except sqlite3.IntegrityError:
                continue
            return {"id": device_id, "name": name, "code": code,
                    "expires_at": _later(PAIR_TTL_SECONDS)}
        raise RuntimeError("could not mint a unique pairing code")

    def claim_pair_code(self, raw_code: str):
        """Redeem a code once. Returns (device row, token) or None.

        The claim is a single UPDATE with the code and the expiry in its WHERE
        clause, so "is this code still good" and "use it up" are one atomic
        step. Two devices racing on the same code cannot both win, and a code
        that has expired cannot be claimed by the request that was already in
        flight when it did.

        The token is returned here and never again: only its hash is stored.
        """
        code = normalize_pair_code(raw_code)
        if len(code) != PAIR_CODE_LEN:
            return None
        token = secrets.token_urlsafe(32)
        token_hash = hash_device_token(token)
        con = self.connect()
        with con:
            cur = con.execute(
                "UPDATE devices SET token_hash = ?, pair_code = NULL,"
                " pair_expires = NULL, paired_at = ?"
                " WHERE pair_code = ? AND pair_expires > ?",
                (token_hash, _now(), code, _now()),
            )
        if cur.rowcount != 1:
            return None
        row = con.execute(
            "SELECT * FROM devices WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        return row, token

    def device_by_token(self, token: str):
        """The device holding this token, or None.

        A plain indexed lookup on the hash. No constant-time compare: the input
        is 256 bits of randomness, so there is no sequence of guesses that a
        timing difference would help along.
        """
        if not token:
            return None
        return self.connect().execute(
            "SELECT * FROM devices WHERE token_hash = ?", (hash_device_token(token),)
        ).fetchone()

    def devices(self, table_id: str) -> list:
        return self.connect().execute(
            "SELECT * FROM devices WHERE table_id = ? ORDER BY created_at",
            (table_id,),
        ).fetchall()

    def device_in_table(self, table_id: str, device_id: str):
        return self.connect().execute(
            "SELECT * FROM devices WHERE id = ? AND table_id = ?",
            (device_id, table_id),
        ).fetchone()

    def touch_device(self, device_id: str) -> None:
        """Record that this device was last seen now. Best-effort: a puppet
        connecting must not fail because a bookkeeping write did."""
        try:
            con = self.connect()
            with con:
                con.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?",
                            (_now(), device_id))
        except sqlite3.Error:
            pass

    def delete_device(self, table_id: str, device_id: str) -> bool:
        """Forget a device. Its token stops working on its next connect --
        which is what "my cat was stolen" needs to mean."""
        con = self.connect()
        with con:
            cur = con.execute("DELETE FROM devices WHERE id = ? AND table_id = ?",
                              (device_id, table_id))
        return cur.rowcount > 0

    def expire_pair_codes(self) -> int:
        """Delete pending devices whose code ran out. Returns how many.

        Only ever pending ones: `token_hash IS NULL` is the whole safety of
        this statement, and a paired puppet has no expiry to run out of.
        """
        con = self.connect()
        with con:
            cur = con.execute(
                "DELETE FROM devices WHERE token_hash IS NULL AND pair_expires <= ?",
                (_now(),),
            )
        return cur.rowcount

    # -- erasure -------------------------------------------------------------
    #
    # Foreign keys cascade members and usage from a table, and tables from a
    # user, so the SQL below is short. What it cannot reach is the two things
    # that are not rows: the table's directory on disk, and its rows in the
    # `search` index (a virtual table takes no foreign keys). Both are cleared
    # explicitly -- see server.delete_table_completely(), which is the only
    # thing that should call these.

    def delete_table(self, table_id: str) -> bool:
        """Remove a table and, by cascade, its members and usage. Does NOT
        touch the table's files or its search index. Returns False if there
        was no such table."""
        self.clear_index(table_id)
        with self.connect() as con:
            cur = con.execute("DELETE FROM tables WHERE id = ?", (table_id,))
        return cur.rowcount > 0

    def delete_user(self, user_id: str) -> list:
        """Remove a user and every table they own. Returns the ids of the
        tables that went with them, so the caller can delete their directories
        -- the database cannot, and a cascade that leaves a stranger's session
        logs on the disk is not a deletion."""
        owned = [r["id"] for r in self.connect().execute(
            "SELECT id FROM tables WHERE owner_id = ?", (user_id,)).fetchall()]
        for table_id in owned:
            self.clear_index(table_id)
        with self.connect() as con:
            con.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return owned

    # -- search --------------------------------------------------------------

    def index_doc(self, table_id: str, kind: str, ref: str, text: str,
                  date: str = "", title: str = "") -> None:
        """Upsert one searchable document. Replaces any previous document with
        the same (table_id, kind, ref), so re-indexing an NPC whose description
        changed every fold-in leaves one row rather than twenty.

        The delete is a full scan of the index -- fts5 cannot index the
        UNINDEXED columns it matches on. At a home game's scale (a few thousand
        rows) that is nothing. If it ever stops being nothing, the upgrade is
        the external-content pattern: a plain table with a real primary key,
        and triggers keeping the fts5 index in step.
        """
        if not text or not text.strip():
            self.unindex_doc(table_id, kind, ref)
            return
        with self.connect() as con:
            con.execute("DELETE FROM search WHERE table_id = ? AND kind = ? AND ref = ?",
                        (table_id, kind, ref))
            con.execute(
                "INSERT INTO search (text, table_id, kind, ref, date, title)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (text, table_id, kind, ref, date, title))

    def unindex_doc(self, table_id: str, kind: str, ref: str) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM search WHERE table_id = ? AND kind = ? AND ref = ?",
                        (table_id, kind, ref))

    def clear_index(self, table_id: str, kind: str = "") -> None:
        """Drop one table's index, or one kind of it. Used by
        tools/reindex_search.py before a rebuild."""
        sql = "DELETE FROM search WHERE table_id = ?"
        args = [table_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        with self.connect() as con:
            con.execute(sql, args)

    def search(self, table_id: str, query: str, kind: str = "", limit: int = 40) -> list:
        """Ranked matches within one table. `query` is raw user input; it goes
        through fts_query() here so no caller can forget to.

        Ordered by bm25, which is negative-is-better, so a plain ASC ordering
        puts the best match first."""
        match = fts_query(query)
        if not match:
            return []
        sql = ("SELECT kind, ref, date, title,"
               f"       snippet(search, 0, '{MARK_START}', '{MARK_END}', '…', 14) AS snippet,"
               "       bm25(search) AS rank"
               "  FROM search"
               " WHERE search MATCH ? AND table_id = ?")
        args = [match, table_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY rank LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        with self.connect() as con:
            return con.execute(sql, args).fetchall()

    def search_counts(self, table_id: str, query: str) -> dict:
        """How many matches of each kind, for the filter chips. A separate
        query rather than counting the limited result set, so the chips do not
        lie about how much is behind them."""
        match = fts_query(query)
        if not match:
            return {}
        with self.connect() as con:
            rows = con.execute(
                "SELECT kind, COUNT(*) AS n FROM search"
                " WHERE search MATCH ? AND table_id = ? GROUP BY kind",
                (match, table_id)).fetchall()
        return {r["kind"]: r["n"] for r in rows}


_db: Database | None = None
_db_lock = threading.Lock()


def get_db(path=None) -> Database:
    """Process-wide database handle. `path` is honoured on first call only."""
    global _db
    with _db_lock:
        if _db is None:
            _db = Database(path or os.environ.get("APP_DB") or "data/app.db")
        return _db


def reset_db_for_tests(path) -> Database:
    global _db
    with _db_lock:
        _db = Database(path)
        return _db
