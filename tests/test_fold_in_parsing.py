"""Regression net for the pure parsing helpers in server.py.

They have no I/O and no table scope, so their behaviour can be pinned exactly.

_split_fold_in_response is the fragile one: the model is known to drift from
the requested markdown heading to a bare divider.
"""

import json

import pytest

import server


# --------------------------------------------------------------------------
# _split_fold_in_response
# --------------------------------------------------------------------------

BOND = "## Party & relationships\nThe party trusts the familiar."
MEMORY = "## Ongoing threads\nFind the amulet.\n\n## Recent developments\nThey reached the bridge."
HEAD = f"{BOND}\n\n{MEMORY}"

CAST = "## Cast updates\n- Yarrow: a nervous hedge witch."
EVENTS = "## New events\n- The party crossed the river."


def test_split_no_permanent_sections_is_bond_plus_memory():
    fold = server._split_fold_in_response(HEAD)
    assert fold.bond == BOND
    assert fold.memory == MEMORY
    assert fold.cast == ""
    assert fold.events == ""


def test_split_all_four_sections_in_prompt_order():
    fold = server._split_fold_in_response(f"{HEAD}\n\n{CAST}\n\n{EVENTS}\n")
    assert fold.bond == BOND
    assert fold.memory == MEMORY
    assert "Yarrow" in fold.cast and "New events" not in fold.cast
    assert "crossed the river" in fold.events and "Yarrow" not in fold.events


def test_split_handles_reversed_permanent_section_order():
    """The prompt asks for cast-then-events, but the split must not depend on it."""
    fold = server._split_fold_in_response(f"{HEAD}\n\n{EVENTS}\n\n{CAST}\n")
    assert fold.memory == MEMORY
    assert "Yarrow" in fold.cast and "crossed the river" not in fold.cast
    assert "crossed the river" in fold.events and "Yarrow" not in fold.events


def test_split_cast_only():
    fold = server._split_fold_in_response(f"{HEAD}\n\n{CAST}\n")
    assert fold.memory == MEMORY
    assert "Yarrow" in fold.cast
    assert fold.events == ""


def test_split_events_only():
    fold = server._split_fold_in_response(f"{HEAD}\n\n{EVENTS}\n")
    assert fold.memory == MEMORY
    assert fold.cast == ""
    assert "crossed the river" in fold.events


@pytest.mark.parametrize(
    "heading",
    ["## Cast updates", "# Cast updates", "###### Cast updates", "## cast updates", "## CAST UPDATES"],
)
def test_split_cast_heading_level_and_case_insensitive(heading):
    fold = server._split_fold_in_response(f"{HEAD}\n\n{heading}\n- Yarrow: a hedge witch.\n")
    assert fold.memory == MEMORY
    assert "Yarrow" in fold.cast


def test_split_divider_fallback():
    """Observed drift: the model emits a bare divider instead of a heading."""
    fold = server._split_fold_in_response(f"{HEAD}\n\n--- cast updates ---\n- Yarrow: a hedge witch.\n")
    assert fold.memory == MEMORY
    assert "Yarrow" in fold.cast


def test_split_unrecognised_heading_is_treated_as_absent():
    """A heading matching neither pattern must leave the section empty rather
    than risk misparsing -- callers read empty as 'nothing this flush'."""
    fold = server._split_fold_in_response(f"{HEAD}\n\n## Cast changes\n- Yarrow: a hedge witch.\n")
    assert fold.cast == ""
    assert fold.events == ""
    assert "Cast changes" in fold.memory, "unparsed section stays in memory, not dropped"


def test_split_empty_string():
    """Every destination empty -- compared field by field rather than as a
    tuple, so adding a sixth section is a one-line change here instead of a
    puzzling failure."""
    fold = server._split_fold_in_response("")
    assert not any(fold)


# --------------------------------------------------------------------------
# _split_fold_in_response -- the bond section
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "heading",
    ["## Party & relationships", "# Party & relationships", "## party and relationships",
     "## PARTY & RELATIONSHIPS", "###### Party & Relationships"],
)
def test_bond_heading_variants(heading):
    fold = server._split_fold_in_response(f"{heading}\nThey trust it.\n\n{MEMORY}")
    assert "They trust it." in fold.bond
    assert fold.memory == MEMORY


def test_bond_divider_fallback():
    fold = server._split_fold_in_response(f"--- party & relationships ---\nThey trust it.\n\n{MEMORY}")
    assert "They trust it." in fold.bond
    assert fold.memory == MEMORY


def test_bond_absent_leaves_bond_empty_and_memory_whole():
    """An absent bond section must not blank the stored bond -- callers only
    write when it is non-empty."""
    fold = server._split_fold_in_response(MEMORY)
    assert fold.bond == ""
    assert fold.memory == MEMORY


def test_bond_stops_at_the_next_heading_of_any_level():
    text = "## Party & relationships\nThey trust it.\n### Aside\nnot bond\n"
    fold = server._split_fold_in_response(text)
    assert "They trust it." in fold.bond
    assert "not bond" not in fold.bond
    assert "not bond" in fold.memory


def test_bond_does_not_leak_into_permanent_sections():
    fold = server._split_fold_in_response(f"{BOND}\n\n{CAST}\n\n{EVENTS}")
    assert fold.bond == BOND
    assert fold.memory == ""
    assert "trusts" not in fold.cast and "trusts" not in fold.events


def test_bond_mention_after_new_events_is_not_treated_as_bond():
    """A stray '## Party & relationships' inside the events section belongs to
    that section -- lifting it out would corrupt the permanent record."""
    text = f"{MEMORY}\n\n{EVENTS}\n## Party & relationships\nstray\n"
    fold = server._split_fold_in_response(text)
    assert fold.bond == ""
    assert "stray" in fold.events
    assert fold.memory == MEMORY


def test_bond_is_not_capped_with_memory():
    """_cap_words is applied to memory only -- bond has its own small file."""
    fold = server._split_fold_in_response(HEAD)
    assert server._cap_words(fold.memory, 3).startswith("## Ongoing threads")
    assert "trusts the familiar" in fold.bond


# --------------------------------------------------------------------------
# _cap_words
# --------------------------------------------------------------------------

def test_cap_words_under_limit_is_unchanged():
    text = "one two three"
    assert server._cap_words(text, 5) == text


def test_cap_words_at_limit_is_unchanged():
    text = "one two three"
    assert server._cap_words(text, 3) == text


def test_cap_words_over_limit_truncates_and_marks():
    capped = server._cap_words("one two three four five", 3)
    assert capped.startswith("one two three")
    assert "trimmed for length" in capped
    assert "four" not in capped


def test_cap_words_normalises_whitespace_when_truncating():
    capped = server._cap_words("one\n\ntwo    three  four", 2)
    assert capped.startswith("one two")


def test_cap_words_empty():
    assert server._cap_words("", 10) == ""


# --------------------------------------------------------------------------
# _extract_tags  (rolls, rests and weather on a timeline bullet)
# --------------------------------------------------------------------------

def test_extract_tags_wellformed_roll():
    clean, tags = server._extract_tags(
        "Tam picked the lock. {roll: character=Tam, outcome=success}"
    )
    assert clean == "Tam picked the lock."
    assert tags == {"roll": {"character": "Tam", "outcome": "success"}}


@pytest.mark.parametrize("outcome", sorted(server.ROLL_OUTCOMES))
def test_extract_tags_accepts_every_valid_outcome(outcome):
    _, tags = server._extract_tags(f"An event. {{roll: character=Tam, outcome={outcome}}}")
    assert tags["roll"]["outcome"] == outcome


@pytest.mark.parametrize("rest", sorted(server.REST_TYPES))
def test_extract_tags_accepts_every_rest_type(rest):
    clean, tags = server._extract_tags(f"They made camp. {{rest: {rest}}}")
    assert clean == "They made camp."
    assert tags == {"rest": rest}


def test_extract_tags_reads_weather_as_free_text():
    clean, tags = server._extract_tags("It poured all afternoon. {weather: heavy rain}")
    assert clean == "It poured all afternoon."
    assert tags == {"weather": "heavy rain"}


def test_extract_tags_caps_a_runaway_weather_tag():
    """Free text, but not unbounded -- a model that decides to narrate the sky
    must not be able to write an essay into the record."""
    _, tags = server._extract_tags("Event. {weather: " + "grey " * 100 + "}")
    assert len(tags["weather"]) <= server.WEATHER_MAX_CHARS


def test_extract_tags_lowercases_outcome_and_rest():
    _, tags = server._extract_tags("An event. {roll: character=Tam, outcome=SUCCESS}")
    assert tags["roll"]["outcome"] == "success"
    _, tags = server._extract_tags("An event. {REST: Long}")
    assert tags["rest"] == "long"


def test_extract_tags_absent():
    bullet = "The party crossed the river."
    assert server._extract_tags(bullet) == (bullet, {})


def test_extract_tags_several_on_one_bullet():
    """A roll made in the rain. Tags are peeled right to left until the text
    stops ending in one."""
    clean, tags = server._extract_tags(
        "Tam slipped on the wet stone. "
        "{roll: character=Tam, outcome=failure} {weather: heavy rain}"
    )
    assert clean == "Tam slipped on the wet stone."
    assert tags == {"roll": {"character": "Tam", "outcome": "failure"},
                    "weather": "heavy rain"}


def test_extract_tags_several_without_spaces_between():
    clean, tags = server._extract_tags("They camped. {rest: long}{weather: first frost}")
    assert clean == "They camped."
    assert tags == {"rest": "long", "weather": "first frost"}


def test_extract_tags_a_repeated_tag_resolves_to_the_leftmost():
    """Both are stripped either way; the leftmost is the one the sentence reads
    as owning."""
    clean, tags = server._extract_tags("They rested. {rest: long} {rest: short}")
    assert clean == "They rested."
    assert tags == {"rest": "long"}


def test_extract_tags_invalid_outcome_strips_tag_but_records_nothing():
    clean, tags = server._extract_tags("An event. {roll: character=Tam, outcome=banana}")
    assert clean == "An event."
    assert tags == {}


def test_extract_tags_invalid_rest_strips_tag_but_records_nothing():
    clean, tags = server._extract_tags("An event. {rest: elevenses}")
    assert clean == "An event."
    assert tags == {}


def test_extract_tags_empty_character_strips_tag_but_records_nothing():
    clean, tags = server._extract_tags("An event. {roll: character= , outcome=success}")
    assert clean == "An event."
    assert tags == {}


def test_extract_tags_a_malformed_tag_does_not_hide_a_good_one_before_it():
    """The bad tag is peeled off, and peeling continues."""
    clean, tags = server._extract_tags("They camped. {rest: long} {weather: }")
    assert clean == "They camped."
    assert tags == {"rest": "long"}


def test_extract_tags_unknown_kind_is_left_in_the_text():
    """Only the known vocabulary is a tag. Anything else is prose the model
    wrote, and eating it would lose part of the event."""
    bullet = "They argued about it. {mood: tense}"
    assert server._extract_tags(bullet) == (bullet, {})


@pytest.mark.parametrize("bullet", [
    "{roll: character=Tam, outcome=success} then they ran.",
    "They discussed a {rest: long} and decided against it.",
])
def test_extract_tags_only_matches_at_the_end(bullet):
    """A tag mid-sentence is prose, not data. Guessing otherwise would put the
    model's asides into the permanent record -- and for a rest, would advance
    the in-game clock on a rest the party never took."""
    assert server._extract_tags(bullet) == (bullet, {})


# --------------------------------------------------------------------------
# _slugify
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("Yarrow", "yarrow"),
        ("Sister Yarrow", "sister-yarrow"),
        ("  Sister   Yarrow  ", "sister-yarrow"),
        ("Yarrow, the Hedge Witch", "yarrow-the-hedge-witch"),
        ("O'Malley", "o-malley"),
        ("Grix-7", "grix-7"),
        ("---Yarrow---", "yarrow"),
    ],
)
def test_slugify(name, expected):
    assert server._slugify(name) == expected


@pytest.mark.parametrize("name", ["", "   ", "!!!", "-", "???"])
def test_slugify_falls_back_to_the_callers_noun(name):
    """A name that slugifies to nothing still needs a key. Which word it falls
    back to belongs to the caller, because the same function keys both the NPC
    cast and the list of places."""
    assert server._slugify(name, "npc") == "npc"
    assert server._slugify(name, "place") == "place"


def test_each_roster_brings_its_own_fallback():
    assert server.CAST.noun == "npc"
    assert server.LOCATIONS.noun == "place"


def test_slugify_is_stable_under_reapplication():
    once = server._slugify("Sister Yarrow, the Hedge Witch")
    assert server._slugify(once) == once


# --------------------------------------------------------------------------
# _resolve_timeline_events  (reads the table's sessions dir, under tmp_path)
# --------------------------------------------------------------------------

@pytest.fixture
def sessions(make_table):
    """An isolated table whose session directory the test writes into.

    `sessions.dir` is where the log files go; `sessions.events(day)` is the
    call under test. Going through a real Table rather than monkeypatching a
    module global means the test exercises the same path resolution production
    does."""
    table, _rt = make_table()

    class Sessions:
        dir = table.sessions_dir

        def __truediv__(self, name):        # so `sessions / "x.log"` still reads well
            return table.sessions_dir / name

        def events(self, day):
            return server._resolve_timeline_events(table, day)

    return Sessions()


def _write(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


DAY = "2026-08-24"


def test_resolve_timeline_missing_file_is_empty(sessions):
    assert sessions.events(DAY) == []


def test_resolve_timeline_without_amendments(sessions):
    _write(sessions / f"{DAY}-timeline.log", [
        {"t": "2026-08-24T19:00:00+00:00", "id": f"{DAY}-0000", "event": "The party crossed the river."},
    ])
    (event,) = sessions.events(DAY)
    assert event["id"] == f"{DAY}-0000"
    assert event["original"] == "The party crossed the river."
    assert event["current"] == "The party crossed the river."
    assert event["roll"] is None
    assert event["amendments"] == []


def test_resolve_timeline_recomputes_missing_id(sessions):
    """Lines written before `id` existed must still resolve to the same id."""
    _write(sessions / f"{DAY}-timeline.log", [
        {"t": "2026-08-24T19:00:00+00:00", "event": "first"},
        {"t": "2026-08-24T19:05:00+00:00", "event": "second"},
    ])
    events = sessions.events(DAY)
    assert [e["id"] for e in events] == [f"{DAY}-0000", f"{DAY}-0001"]


def test_resolve_timeline_latest_amendment_wins(sessions):
    _write(sessions / f"{DAY}-timeline.log", [
        {"t": "2026-08-24T19:00:00+00:00", "id": f"{DAY}-0000", "event": "The party crossed the river."},
    ])
    _write(sessions / f"{DAY}-amendments.log", [
        {"event_index": 0, "text": "The party forded the river.", "by": "Alex", "at": "2026-08-24T21:00:00+00:00"},
        {"event_index": 0, "text": "The party swam the river.", "by": "Sam", "at": "2026-08-24T22:00:00+00:00"},
    ])
    (event,) = sessions.events(DAY)
    assert event["original"] == "The party crossed the river."
    assert event["current"] == "The party swam the river."
    assert [a["by"] for a in event["amendments"]] == ["Alex", "Sam"]


def test_resolve_timeline_latest_roll_amendment_wins(sessions):
    _write(sessions / f"{DAY}-timeline.log", [
        {"t": "2026-08-24T19:00:00+00:00", "id": f"{DAY}-0000", "event": "Tam picked the lock.",
         "roll": {"character": "Tam", "outcome": "success"}},
    ])
    _write(sessions / f"{DAY}-amendments.log", [
        {"event_index": 0, "text": "Tam picked the lock.", "by": "Alex", "at": "2026-08-24T21:00:00+00:00",
         "roll": {"character": "Tam", "outcome": "crit"}},
    ])
    (event,) = sessions.events(DAY)
    assert event["roll"] == {"character": "Tam", "outcome": "crit"}


def test_resolve_timeline_text_only_amendment_keeps_original_roll(sessions):
    _write(sessions / f"{DAY}-timeline.log", [
        {"t": "2026-08-24T19:00:00+00:00", "id": f"{DAY}-0000", "event": "Tam picked the lock.",
         "roll": {"character": "Tam", "outcome": "success"}},
    ])
    _write(sessions / f"{DAY}-amendments.log", [
        {"event_index": 0, "text": "Tam forced the lock.", "by": "Alex", "at": "2026-08-24T21:00:00+00:00"},
    ])
    (event,) = sessions.events(DAY)
    assert event["current"] == "Tam forced the lock."
    assert event["roll"] == {"character": "Tam", "outcome": "success"}


def test_resolve_timeline_amendments_key_on_position_not_stored_id(sessions):
    """Amendments index by line position -- the constraint that makes the
    single-worker deployment load-bearing for data integrity."""
    _write(sessions / f"{DAY}-timeline.log", [
        {"t": "2026-08-24T19:00:00+00:00", "id": f"{DAY}-0000", "event": "first"},
        {"t": "2026-08-24T19:05:00+00:00", "id": f"{DAY}-0001", "event": "second"},
    ])
    _write(sessions / f"{DAY}-amendments.log", [
        {"event_index": 1, "text": "second, corrected", "by": "Alex", "at": "2026-08-24T21:00:00+00:00"},
    ])
    first, second = sessions.events(DAY)
    assert first["current"] == "first"
    assert second["current"] == "second, corrected"


def test_resolve_timeline_skips_blank_and_corrupt_lines(sessions):
    (sessions / f"{DAY}-timeline.log").write_text(
        json.dumps({"t": "x", "id": f"{DAY}-0000", "event": "good"}) + "\n"
        "\n"
        "not json at all\n"
        + json.dumps({"t": "y", "id": f"{DAY}-0002", "event": "also good"}) + "\n",
        encoding="utf-8",
    )
    events = sessions.events(DAY)
    assert [e["current"] for e in events] == ["good", "also good"]


def test_resolve_timeline_ignores_corrupt_amendment_lines(sessions):
    _write(sessions / f"{DAY}-timeline.log", [
        {"t": "x", "id": f"{DAY}-0000", "event": "original"},
    ])
    (sessions / f"{DAY}-amendments.log").write_text(
        "not json\n"
        + json.dumps({"no_event_index": 0, "text": "ignored"}) + "\n"
        + json.dumps({"event_index": 0, "text": "kept", "by": "Alex", "at": "t"}) + "\n",
        encoding="utf-8",
    )
    (event,) = sessions.events(DAY)
    assert event["current"] == "kept"
