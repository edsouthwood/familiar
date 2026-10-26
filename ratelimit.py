"""How often a stranger may knock.

With the reverse proxy's shared password gone, the doors that take no cookie --
login, signup, pairing, joining -- are reachable by anyone, and each of them is
guarded by a secret somebody might try to guess: a password, the signup code
(which an operator picks, so it may be a word), a pairing code, an invite token.
None of those is weak against a handful of guesses. All of them are weaker
against a script making thousands.

In-process and hand-rolled, for the same reason the body bus is: the service
runs as exactly one gunicorn process (see deploy/familiar.service), so a dict
and a lock is the whole of the shared state, and a restart forgetting every
count is fine -- the windows are minutes long. A second worker would give each
its own counts, which would only make the limits looser, never wrong.
"""

import threading
import time
from collections import deque

# Bounds memory against a flood from many addresses. Past this, the key whose
# most recent attempt is oldest is forgotten first -- an attacker spraying
# addresses can at worst push out other idle strangers, never a live window.
MAX_KEYS = 10_000


class RateLimit:
    """At most `limit` recorded attempts per key in any `window` seconds.

    Checking and recording are separate on purpose. Login records only
    failures -- a DM signing in five times on five devices is not an attack --
    while signup records every attempt, because a *successful* signup is the
    expensive outcome there.
    """

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float):
        hits = self._hits.get(key)
        if hits is None:
            return None
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if not hits:
            del self._hits[key]
            return None
        return hits

    def retry_after(self, key: str) -> int:
        """Seconds until `key` may try again, or 0 if it may try now."""
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, now)
            if hits is None or len(hits) < self.limit:
                return 0
            return max(1, int(hits[0] + self.window - now) + 1)

    def record(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, now)
            if hits is None:
                if len(self._hits) >= MAX_KEYS:
                    stalest = min(self._hits, key=lambda k: self._hits[k][-1])
                    del self._hits[stalest]
                hits = self._hits[key] = deque(maxlen=self.limit)
            hits.append(now)

    def reset(self) -> None:
        """Forget everything. For tests, which all arrive from 127.0.0.1."""
        with self._lock:
            self._hits.clear()


# Per client address unless named otherwise. The numbers are set so that a
# person who mistypes never meets them, and a script meets them within seconds.
LOGIN_FAILURES_PER_IP = RateLimit(10, 15 * 60)
# Per account, across every address: the brake on a guesser with many IPs.
# Looser than the per-address one, because the cost of hitting it falls on the
# account's owner, who is then locked out of signing in for up to an hour.
LOGIN_FAILURES_PER_EMAIL = RateLimit(30, 60 * 60)
SIGNUP_ATTEMPTS_PER_IP = RateLimit(5, 60 * 60)
PAIR_FAILURES_PER_IP = RateLimit(10, 15 * 60)
JOIN_FAILURES_PER_IP = RateLimit(20, 60 * 60)

ALL = (LOGIN_FAILURES_PER_IP, LOGIN_FAILURES_PER_EMAIL, SIGNUP_ATTEMPTS_PER_IP,
       PAIR_FAILURES_PER_IP, JOIN_FAILURES_PER_IP)


def reset_all() -> None:
    for limit in ALL:
        limit.reset()
