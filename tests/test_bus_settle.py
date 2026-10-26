"""What the body bus holds when the screen driving it goes away.

Written after a puppet flapped its jaw for fourteen minutes. A tablet broadcast
`speaking`, then vanished mid-utterance; the server kept handing that state to
every body, and the cat went on mouthing an answer that had stopped existing.
Nothing had crashed — the bus was asserting something stale with confidence.
"""

import json
import uuid

import server


class FakeBody:
    """A body on the bus without a socket. `sent` is what it was told."""

    def __init__(self, may_drive=True):
        self.may_drive = may_drive
        self.sent = []

    def send(self, data):
        self.sent.append(json.loads(data))

    def close(self):
        pass


def _rt(state="speaking"):
    """A runtime nobody else shares.

    uuid4 rather than id(object()): the temporary is freed immediately, so its
    address gets reused and two tests end up on the same runtime — one of them
    inheriting the other's bodies. That passed in isolation and failed in a
    full run, which is the most expensive way to learn it.
    """
    rt = server.get_runtime(f"settle-{state}-{uuid.uuid4()}")
    rt.current_state = state
    return rt


def test_the_last_screen_leaving_puts_the_bus_to_rest():
    rt = _rt("speaking")
    puppet = FakeBody(may_drive=False)
    rt.add_body(puppet)

    assert server.settle_bus(rt) is True
    assert rt.current_state == server.REST_STATE
    # And the puppet is *told*, rather than left holding the old pose.
    assert puppet.sent == [{"type": "state", "state": server.REST_STATE}]


def test_a_puppet_alone_cannot_hold_the_bus_awake():
    """The heart of it: a body that only listens is not somebody being spoken
    to. Counting it as a driver is what would have kept the jaw moving."""
    rt = _rt("thinking")
    rt.add_body(FakeBody(may_drive=False))
    rt.add_body(FakeBody(may_drive=False))
    assert server.settle_bus(rt) is True
    assert rt.current_state == server.REST_STATE


def test_a_screen_still_on_the_bus_keeps_it_awake():
    rt = _rt("speaking")
    screen = FakeBody(may_drive=True)
    rt.add_body(screen)
    rt.add_body(FakeBody(may_drive=False))

    assert server.settle_bus(rt) is False
    assert rt.current_state == "speaking"
    assert screen.sent == []


def test_one_of_two_screens_leaving_changes_nothing():
    """Two tablets at one table. Closing a laptop must not put the cat to sleep
    in the middle of somebody's question."""
    rt = _rt("listening")
    rt.add_body(FakeBody(may_drive=True))   # the one that stayed
    assert server.settle_bus(rt) is False
    assert rt.current_state == "listening"


def test_settling_an_empty_bus_at_rest_says_nothing():
    """Idempotent: a body leaving an already-asleep table is not an event, and
    should not spray a redundant broadcast at everyone each time."""
    rt = _rt(server.REST_STATE)
    assert server.settle_bus(rt) is False


def test_an_empty_bus_settles_even_with_nobody_to_tell():
    """No bodies at all. The state still has to be corrected, because the next
    body to connect is handed it as a snapshot — which is exactly how a puppet
    powering on inherits a pose from a session that ended hours ago."""
    rt = _rt("speaking")
    assert server.settle_bus(rt) is True
    assert rt.current_state == server.REST_STATE


def test_a_body_records_whether_it_may_drive():
    """_Body carries it because settle_bus runs *after* the connection ends,
    when the seat that knew is gone."""
    class Dummy:
        def send(self, data): pass
        def close(self): pass

    assert server._Body(Dummy()).may_drive is True
    assert server._Body(Dummy(), may_drive=False).may_drive is False
