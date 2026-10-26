"""The in-game clock, and correcting it.

Long rests drive a day counter. Nothing about the calendar is stored: it is
recomputed from the amendment-resolved timeline on every request, the same way
api_stats recomputes dice tallies. That is the point -- the familiar will
sometimes hear a party wondering aloud about a long rest and record one they
never took, and the fix has to be an ordinary amendment with no separate
invalidation step to forget.
"""

from datetime import datetime, timezone

import pytest

import server


def _at(day: str, hour: int = 19) -> datetime:
    return datetime(*(int(x) for x in day.split("-")), hour, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def chronicle(make_table):
    """A table you can write dated timeline events into."""
    table, rt = make_table()

    class Chronicle:
        def __init__(self):
            self.table, self.rt = table, rt

        def record(self, day: str, *bullets):
            server._append_timeline(table, rt,
                                    "\n".join(f"- {b}" for b in bullets),
                                    when=_at(day))

        def days(self):
            return server._campaign_days(table)[1]

        def event_days(self):
            return server._campaign_days(table)[0]

        def events(self, day):
            return server._resolve_timeline_events(table, day)

    return Chronicle()


# ------------------------------------------------------------ the counter ---

def test_a_campaign_with_no_rests_is_on_day_one(chronicle):
    """Not "no day at all" -- the party has been somewhere, it just hasn't
    slept yet."""
    chronicle.record("2026-08-21", "The party crossed the bridge.")
    days = chronicle.days()
    assert [d["day"] for d in days] == [1]
    assert days[0]["ended_by"] is None


def test_an_empty_campaign_still_reports_day_one(chronicle):
    assert [d["day"] for d in chronicle.days()] == [1]


def test_a_long_rest_ends_the_day_it_appears_on(chronicle):
    """The rest is the last thing that happens on day 1, not the first thing
    on day 2 -- a party makes camp at the end of a day."""
    chronicle.record("2026-08-21",
                     "The party crossed the bridge.",
                     "They made camp by the ford. {rest: long}",
                     "At dawn they found tracks.")
    ids = [e["id"] for e in chronicle.events("2026-08-21")]
    days = chronicle.event_days()
    assert [days[i] for i in ids] == [1, 1, 2]


def test_a_short_rest_does_not_move_the_clock(chronicle):
    chronicle.record("2026-08-21",
                     "They caught their breath. {rest: short}",
                     "Then pressed on.")
    assert [d["day"] for d in chronicle.days()] == [1]


def test_several_rests_advance_one_day_each(chronicle):
    chronicle.record("2026-08-21",
                     "Day one happened. {rest: long}",
                     "Day two happened. {rest: long}",
                     "Day three is under way.")
    assert [d["day"] for d in chronicle.days()] == [1, 2, 3]


def test_days_carry_across_session_boundaries(chronicle):
    """The clock is the campaign's, not the evening's. A day that starts in one
    session and ends in the next is one day."""
    chronicle.record("2026-08-21", "They set out.", "They camped. {rest: long}")
    chronicle.record("2026-08-28", "They walked on.", "They camped again. {rest: long}")
    chronicle.record("2026-09-04", "They reached the city.")

    days = chronicle.days()
    assert [d["day"] for d in days] == [1, 2, 3]
    assert days[0]["sessions"] == ["2026-08-21"]
    assert days[2]["sessions"] == ["2026-09-04"]


def test_a_day_spanning_two_sessions_lists_both(chronicle):
    chronicle.record("2026-08-21", "They set out.")
    chronicle.record("2026-08-28", "Still the same day. {rest: long}")
    assert chronicle.days()[0]["sessions"] == ["2026-08-21", "2026-08-28"]


# --------------------------------------------------------------- weather ----

def test_weather_is_collected_per_day(chronicle):
    chronicle.record("2026-08-21",
                     "It rained all morning. {weather: heavy rain}",
                     "They camped wet through. {rest: long}",
                     "Clear skies at dawn. {weather: clear}")
    days = chronicle.days()
    assert days[0]["weather"] == ["heavy rain"]
    assert days[1]["weather"] == ["clear"]


def test_repeated_weather_within_a_day_is_recorded_once(chronicle):
    chronicle.record("2026-08-21",
                     "Rain. {weather: heavy rain}",
                     "Still raining. {weather: heavy rain}")
    assert chronicle.days()[0]["weather"] == ["heavy rain"]


def test_a_bullet_can_carry_a_rest_and_the_weather_together(chronicle):
    chronicle.record("2026-08-21", "They camped in the snow. {rest: long}{weather: first snow}")
    days = chronicle.days()
    assert days[0]["weather"] == ["first snow"]
    assert days[0]["ended_by"] is not None


# ------------------------------------------------------------ correcting ----

def test_amending_a_rest_away_shifts_the_calendar_back(chronicle):
    """The correction this whole design exists for: the party said "should we
    long rest?" and then didn't."""
    chronicle.record("2026-08-21",
                     "They talked about resting. {rest: long}",
                     "Then pressed on regardless.")
    assert [d["day"] for d in chronicle.days()] == [1, 2]

    server._append_amendment(chronicle.table, chronicle.rt, "2026-08-21", 0,
                             "They talked about resting.", "Rowan", {"rest": None})

    assert [d["day"] for d in chronicle.days()] == [1]
    assert chronicle.events("2026-08-21")[0]["rest"] is None


def test_amending_a_rest_in_advances_the_calendar(chronicle):
    chronicle.record("2026-08-21", "They bedded down.", "Morning came.")
    assert [d["day"] for d in chronicle.days()] == [1]

    server._append_amendment(chronicle.table, chronicle.rt, "2026-08-21", 0,
                             "They bedded down for the night.", "Rowan", {"rest": "long"})

    assert [d["day"] for d in chronicle.days()] == [1, 2]


def test_an_amendment_that_mentions_no_tags_leaves_them_alone(chronicle):
    """Fixing a typo in the text must not silently drop the rest."""
    chronicle.record("2026-08-21", "They camped. {rest: long}{weather: rain}")
    server._append_amendment(chronicle.table, chronicle.rt, "2026-08-21", 0,
                             "They made camp.", "Rowan", {})

    event = chronicle.events("2026-08-21")[0]
    assert event["current"] == "They made camp."
    assert event["rest"] == "long"
    assert event["weather"] == "rain"


def test_the_latest_amendment_wins_per_tag(chronicle):
    chronicle.record("2026-08-21", "Something happened. {rest: long}")
    for value in ("short", None, "long"):
        server._append_amendment(chronicle.table, chronicle.rt, "2026-08-21", 0,
                                 "Something happened.", "Rowan", {"rest": value})
    assert chronicle.events("2026-08-21")[0]["rest"] == "long"


def test_clearing_one_tag_leaves_the_others(chronicle):
    chronicle.record("2026-08-21",
                     "Tam slipped in the rain. "
                     "{roll: character=Tam, outcome=failure} {weather: heavy rain}")
    server._append_amendment(chronicle.table, chronicle.rt, "2026-08-21", 0,
                             "Tam slipped in the rain.", "Rowan", {"weather": None})

    event = chronicle.events("2026-08-21")[0]
    assert event["weather"] is None
    assert event["roll"] == {"character": "Tam", "outcome": "failure"}


# -------------------------------------------------------------- requests ----

def _write(live, day, *bullets):
    server._append_timeline(live.table, live.rt,
                            "\n".join(f"- {b}" for b in bullets), when=_at(day))


def test_the_calendar_endpoint_reports_the_current_day(live):
    _write(live, "2026-08-21", "Set out.", "Camped. {rest: long}", "Woke.")
    body = live.client.get(live.url("/calendar")).get_json()

    assert body["current_day"] == 2
    assert body["rests_recorded"] == 1
    assert [d["day"] for d in body["days"]] == [1, 2]


def test_players_can_see_the_calendar(live):
    """It is the party's own campaign -- not DM-only."""
    _write(live, "2026-08-21", "Something happened.")
    assert live.player_client().get(live.url("/calendar")).status_code == 200


def test_the_timeline_endpoint_annotates_each_event_with_its_day(live):
    _write(live, "2026-08-21", "Set out.", "Camped. {rest: long}", "Woke.")
    events = live.client.get(live.url("/sessions/2026-08-21/timeline")).get_json()["events"]
    assert [e["day"] for e in events] == [1, 1, 2]


def test_a_later_sessions_days_account_for_earlier_rests(live):
    """The day number cannot be computed from one session's events alone, which
    is why the endpoint works it out rather than the viewer."""
    _write(live, "2026-08-21", "Camped. {rest: long}")
    _write(live, "2026-08-28", "A new morning.")
    events = live.client.get(live.url("/sessions/2026-08-28/timeline")).get_json()["events"]
    assert [e["day"] for e in events] == [2]


def test_amending_through_the_api_can_clear_a_rest(live):
    _write(live, "2026-08-21", "They discussed resting. {rest: long}")
    assert live.client.get(live.url("/calendar")).get_json()["current_day"] == 2

    r = live.client.post(live.url("/sessions/2026-08-21/timeline/2026-08-21-0000/amend"),
                         json={"text": "They discussed resting.", "rest": None})
    assert r.status_code == 200
    assert r.get_json()["rest"] is None
    assert live.client.get(live.url("/calendar")).get_json()["current_day"] == 1


def test_an_amendment_omitting_a_tag_key_leaves_it_alone(live):
    """Absent means "no opinion"; null means "remove it". The API has to
    distinguish them or a text fix would wipe the rest."""
    _write(live, "2026-08-21", "They camped. {rest: long}")
    r = live.client.post(live.url("/sessions/2026-08-21/timeline/2026-08-21-0000/amend"),
                         json={"text": "They made camp."})
    assert r.get_json()["rest"] == "long"


def test_a_bad_rest_value_is_refused(live):
    _write(live, "2026-08-21", "They camped.")
    r = live.client.post(live.url("/sessions/2026-08-21/timeline/2026-08-21-0000/amend"),
                         json={"text": "They camped.", "rest": "elevenses"})
    assert r.status_code == 400


def test_the_calendar_does_not_cross_between_tables(live):
    other_user = live.db.create_user("o@example.com", "another-long-password")
    other_id = live.db.create_table(other_user, "Blackmoor", "blackmoor")
    _write(live, "2026-08-21", "Camped. {rest: long}", "Woke.")

    from tenancy import Table
    other = Table(live.db.table(other_id), server.DATA_DIR, server.BASE)
    assert server._campaign_days(other)[1][0]["day"] == 1
    assert live.client.get(live.url("/calendar")).get_json()["current_day"] == 2


# ------------------------------------------------- the clock in the prompt ---
#
# Deriving the day is only half the point: the familiar has to be able to
# answer "what day is it?" out loud. _clock_note is what puts the derived
# number in front of the model, and it lives in the *volatile* half of the
# system prompt because it moves with every rest.

def test_a_silent_campaign_gets_no_clock_note(chronicle):
    """Nothing has happened, so there is nothing true to say. Better silence
    than telling the familiar it is day 1 of a campaign that hasn't started."""
    assert server._clock_note(chronicle.table) == ""


def test_a_campaign_underway_is_told_the_day(chronicle):
    chronicle.record("2026-08-21", "The party crossed the bridge.")
    note = server._clock_note(chronicle.table)
    assert "day 1" in note
    assert note.startswith("## The calendar")


def test_the_note_follows_the_rests(chronicle):
    chronicle.record("2026-08-21", "Camped. {rest: long}", "Camped again. {rest: long}")
    assert "day 3" in server._clock_note(chronicle.table)


def test_the_note_carries_recent_weather(chronicle):
    chronicle.record("2026-08-21", "Rode north. {weather: heavy rain}")
    assert "heavy rain" in server._clock_note(chronicle.table)


def test_yesterdays_weather_carries_into_a_fresh_morning(chronicle):
    """The day the rest opened has no weather of its own yet, so the note
    reaches back one day rather than going quiet about it."""
    chronicle.record("2026-08-21", "Snow all afternoon. {weather: snow}",
                     "They made camp. {rest: long}")
    note = server._clock_note(chronicle.table)
    assert "day 2" in note and "snow" in note


def test_amending_a_rest_away_moves_the_note_back(chronicle):
    chronicle.record("2026-08-21", "They talked about camping. {rest: long}")
    assert "day 2" in server._clock_note(chronicle.table)
    server._append_amendment(chronicle.table, chronicle.rt, "2026-08-21", 0,
                             "They talked about camping.", "Rowan", {"rest": None})
    assert "day 1" in server._clock_note(chronicle.table)


def test_the_clock_note_rides_in_the_volatile_half(chronicle):
    """Not the cached prefix: it changes every time the party sleeps, and a
    prefix that moves is a prefix that never caches."""
    chronicle.record("2026-08-21", "Camped. {rest: long}")
    prompt = server.build_system_prompt(chronicle.table)
    assert "day 2" in prompt.volatile
    assert "day 2" not in prompt.stable


def test_a_broken_clock_does_not_break_the_prompt(chronicle, monkeypatch):
    """Bookkeeping must never be the reason a familiar can't answer."""
    def boom(_table):
        raise RuntimeError("timeline unreadable")
    monkeypatch.setattr(server, "_campaign_days", boom)
    assert server._clock_note(chronicle.table) == ""
    assert server.build_system_prompt(chronicle.table).text
