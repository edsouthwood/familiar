"""Per-table paths and per-table mutable state.

Two objects, deliberately separated by lifetime:

* `Table` is *derived*. It wraps a row from the database and computes where that
  table's files live. Making one is free, so request handlers make one per
  request and never worry about staleness.
* `TableRuntime` is *durable*. It holds the things that must survive between
  requests but not between restarts: the rolling conversation history, the
  ambient transcript buffer, the file locks, and the set of connected bodies.
  There is exactly one per table, kept in a registry.

Before multi-tenancy these were the same thing -- module-level globals in
server.py, one set of them, implicitly belonging to the only table there was.
Splitting them is most of the work of Phase 1, and the reason the split is drawn
here rather than by adding a `table_id` argument everywhere is that the *locks*
have to be per-table too. A single `_cast_lock` shared across tenants would be
correct but would serialise every table's fold-ins behind every other's.
"""

import threading
import time
from collections import deque
from pathlib import Path

from familiars import Familiars

# Tenant data lives under <data_root>/tables/<table id>/. The id, not the slug,
# names the directory: slugs are renameable and a rename must not move a
# gigabyte of session logs.
TABLES_DIRNAME = "tables"


class Table:
    """One tenant's identity and paths.

    `row` is the sqlite row; `data_root` and `app_root` are the two roots that
    server.py already keeps apart -- tenant data and application assets. Every
    path the old single-tenant code derived from DATA_DIR is derived here from
    `self.dir` instead, which is what makes the substitution mechanical.
    """

    def __init__(self, row, data_root, app_root):
        self.row = row
        self.id = row["id"]
        self.slug = row["slug"]
        self.name = row["name"]
        self.owner_id = row["owner_id"]
        self.plan = row["plan"]
        self.invite_token = row["invite_token"]
        self.app_root = Path(app_root)
        self.dir = Path(data_root) / TABLES_DIRNAME / self.id

    def __repr__(self) -> str:
        return f"<Table {self.slug!r} id={self.id}>"

    # -- paths ---------------------------------------------------------------

    @property
    def sessions_dir(self) -> Path:
        return self.dir / "sessions"

    @property
    def game_context_file(self) -> Path:
        return self.dir / "game-context.md"

    @property
    def cast_file(self) -> Path:
        return self.dir / "cast.json"

    @property
    def party_file(self) -> Path:
        """The party: who is actually at the table in the fiction.

        Deliberately not a Roster (see server.Party). A cast member is written
        by the machine and corrected by a human; a party member is the other way
        round, and the two want opposite defaults for almost every question.
        """
        return self.dir / "party.json"

    @property
    def dm_only_file(self) -> Path:
        """The DM's secrets, in a file of their own.

        A separate file rather than a section of campaign.md, because that is
        what makes the containment structural: exactly two things open this --
        build_system_prompt and the DM's own editor -- and the memory fold-in
        is not one of them. It replaces a regex that stripped a "## DM only"
        heading out of the campaign text on the way past, where a heading the
        regex did not match leaked into the table's permanent memory with
        nothing to say so.
        """
        return self.dir / "dm-only.md"

    @property
    def dm_passphrase_file(self) -> Path:
        """The phrase that lets dm-only.md into a prompt at all. See
        server.said_passphrase: the gate is code, not an instruction."""
        return self.dir / "dm-passphrase.txt"

    @property
    def locations_file(self) -> Path:
        """Places the party has been, kept exactly like cast.json -- see
        server.Roster, which is the same code serving both."""
        return self.dir / "locations.json"

    @property
    def familiars(self) -> Familiars:
        """This table's familiars. Cheap to build (it is a path plus methods),
        so it is recomputed rather than cached -- a cached one would go stale
        the moment a familiar was added from another request."""
        return Familiars(self.dir, self.app_root)

    def ensure_dirs(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(exist_ok=True)

    def persona_file(self, name: str, optional: bool = False,
                     fallback: bool = True) -> str:
        """Read <name>.md from this table.

        Two independent switches, which is worth spelling out because they read
        as if they were one and are not:

        `fallback` -- whether a missing file falls back to the tracked
        <name>.example.md shipped with the app. The fallback root is
        `app_root`, not the table directory: the example files are application
        defaults living in the repo, and a naive DATA_DIR -> table.dir
        substitution here would give every newly created table a
        FileNotFoundError on its first question. Pass False when the example
        file is guidance written *to a human* rather than content: campaign.md's
        example says "Name each player character: their class, a trait or two",
        which is a sentence the model must never be handed as campaign fact.

        `optional` -- whether nothing existing at all is "" rather than an
        error. It does NOT skip the fallback, and a caller that wanted no
        fallback and reached for `optional` got the example file anyway,
        silently, for as long as one existed.
        """
        path = self.dir / f"{name}.md"
        if not path.exists() and fallback:
            path = self.app_root / f"{name}.example.md"
        if optional and not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def public(self) -> dict:
        return {"id": self.id, "slug": self.slug, "name": self.name}


class TableRuntime:
    """The mutable, in-process state of one table.

    Everything here was a module global in the single-tenant server. Grouping it
    in one object means a table's state can be reasoned about (and reset) as a
    unit, and means adding a second table cannot silently share a lock with the
    first.
    """

    def __init__(self, table_id: str):
        self.table_id = table_id

        # Rolling conversation memory: (question, answer) pairs, trimmed by the
        # ask routes. Lost on restart, by design -- a fresh-minded familiar.
        self.history: list[tuple[str, str]] = []

        # Ambient transcript accumulating toward the fold-in threshold.
        self.ambient_lock = threading.Lock()
        self.ambient_buffer = ""

        # One lock per mutated file. Separate locks, not one big one: a fold-in
        # rewriting game-context.md should not block a timeline amendment.
        self.game_context_lock = threading.Lock()
        self.cast_lock = threading.Lock()
        self.locations_lock = threading.Lock()
        self.campaign_lock = threading.Lock()
        self.party_lock = threading.Lock()
        self.timeline_lock = threading.Lock()
        self.qa_log_lock = threading.Lock()

        # When this table's recent ambient fold-ins fired. A fold-in is the
        # one LLM call any player can trigger just by leaving the "record table
        # talk" toggle on, so it is rate-limited as well as metered -- a stuck
        # recorder in someone's bag should cost a few pounds, not a few hundred.
        self.fold_in_times: deque = deque(maxlen=256)

        # The body bus: every screen and puppet currently showing this table's
        # familiar, plus the last state they were told to hold.
        self.bodies: set = set()
        self.bodies_lock = threading.Lock()
        self.current_state = "asleep"

    def __repr__(self) -> str:
        return (f"<TableRuntime {self.table_id} bodies={len(self.bodies)} "
                f"state={self.current_state!r}>")

    def allow_fold_in(self, per_hour: int) -> bool:
        """Claim one fold-in against this hour's allowance.

        Records the attempt only when it is allowed, so a table that is being
        refused does not push its own window forward and lock itself out for
        longer than an hour."""
        now = time.monotonic()
        cutoff = now - 3600
        with self.ambient_lock:
            while self.fold_in_times and self.fold_in_times[0] < cutoff:
                self.fold_in_times.popleft()
            if len(self.fold_in_times) >= per_hour:
                return False
            self.fold_in_times.append(now)
            return True

    # -- body bus ------------------------------------------------------------

    def add_body(self, body) -> None:
        with self.bodies_lock:
            self.bodies.add(body)

    def remove_body(self, body) -> None:
        with self.bodies_lock:
            self.bodies.discard(body)

    def broadcast(self, data: str, source=None) -> None:
        """Fan a serialised envelope out to every body of *this table* except
        its source. Takes the encoded string so a broadcast to many bodies
        serialises once.

        The snapshot-then-send shape matters: sending while holding the lock
        would let one body's writer stall every other table's connects and
        disconnects.
        """
        with self.bodies_lock:
            targets = [b for b in self.bodies if b is not source]
        for body in targets:
            body.send(data)


class Registry:
    """table_id -> TableRuntime, created on demand.

    A plain dict guarded by a lock. Runtimes are never evicted: they are small,
    and evicting one with a live websocket in it would strand the body.
    """

    def __init__(self):
        self._runtimes: dict[str, TableRuntime] = {}
        self._lock = threading.Lock()

    def get(self, table_id: str) -> TableRuntime:
        with self._lock:
            rt = self._runtimes.get(table_id)
            if rt is None:
                rt = TableRuntime(table_id)
                self._runtimes[table_id] = rt
            return rt

    def peek(self, table_id: str):
        """The runtime if one exists, without creating it. For status views that
        should not conjure state just by looking."""
        with self._lock:
            return self._runtimes.get(table_id)

    def all(self) -> list[TableRuntime]:
        with self._lock:
            return list(self._runtimes.values())

    def drop(self, table_id: str) -> None:
        with self._lock:
            self._runtimes.pop(table_id, None)


registry = Registry()


def get_runtime(table_id: str) -> TableRuntime:
    return registry.get(table_id)
