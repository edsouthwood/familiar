"""The campaign notes parser.

`campaign.md` is a file a person opens in a text editor *and* a form edits.
Neither side owns it, so the parser has to survive whatever the other side did.
The tests here are mostly properties rather than examples, because the input is
prose typed by a tired DM and there is no enumerable set of shapes it takes.

Three properties carry the weight:

  never raises   -- there is no such thing as a malformed campaign note
  never loses    -- every non-heading line comes back in some field
  fails closed   -- a stray "## DM only" never reaches a prompt reference

The last one is the reason this module exists at all. It replaces a regex that
stripped DM-only content out of the text on its way to the fold-in; a heading
the regex did not match leaked the DM's secrets into the table's permanent
memory, and nothing would have said so.
"""

import pytest

import campaign


REAL = """# Campaign notes

> Edit this file freely.

## The party

- Seraphine, a paladin.

## Where we are

In the mine.

## House rules

- Luck rolls are a thing

## DM only

- The innkeeper is a doppelganger.
"""


# ------------------------------------------------------------- the fields ---

def test_a_known_heading_becomes_its_field():
    fields = campaign.parse(REAL)
    assert fields["where"] == "In the mine."
    assert fields["house_rules"] == "- Luck rolls are a thing"


def test_headings_are_matched_loosely():
    """Case and inner whitespace are not the DM's problem."""
    fields = campaign.parse("##   wHeRe   We    ARE\nIn the mine.")
    assert fields["where"] == "In the mine."


def test_an_absent_section_is_empty_not_missing():
    fields = campaign.parse("## Where we are\nHere.")
    assert fields["setting"] == ""
    assert set(fields) == set(campaign.FIELD_KEYS) | {"dm_only_stray"}


def test_a_repeated_heading_accumulates_rather_than_overwriting():
    """Two "House rules" sections is a thing a hand-edited file does. Keeping
    the last one would silently delete the first."""
    fields = campaign.parse("## House rules\nOne.\n\n## House rules\nTwo.")
    assert "One." in fields["house_rules"] and "Two." in fields["house_rules"]


# --------------------------------------------------------------- retiring ---

def test_a_retired_heading_is_kept_verbatim_under_other():
    """The party moved to party.json, but the DM's prose about it is theirs
    until they delete it themselves."""
    fields = campaign.parse(REAL)
    assert "Seraphine, a paladin." in fields["other"]
    assert "The party" in fields["other"]
    assert "party.json" in fields["other"], "say where it went"


def test_an_unknown_heading_keeps_its_heading_and_body():
    fields = campaign.parse("## Ships we have stolen\n\n- The Grey Gull.")
    assert "Ships we have stolen" in fields["other"]
    assert "The Grey Gull." in fields["other"]


def test_a_preserved_heading_is_demoted_so_it_does_not_reparse():
    """Demotion to ### is what makes a round trip stable: the next parse must
    not read the preserved heading as a section of its own."""
    once = campaign.parse("## Ships we have stolen\n\n- The Grey Gull.")
    twice = campaign.parse(campaign.render(once))
    assert once["other"] == twice["other"]


# ------------------------------------------------------------- the secrets ---

def test_a_stray_dm_only_heading_is_quarantined():
    fields = campaign.parse(REAL)
    assert "doppelganger" in fields["dm_only_stray"]
    assert all("doppelganger" not in fields[key] for key in campaign.FIELD_KEYS)


def test_a_stray_secret_never_reaches_a_prompt_reference():
    """The whole point of the module. reference() enumerates FIELDS and nothing
    else, so it cannot grow a DM-only section by accident the way a regex over
    raw text could miss one."""
    assert "doppelganger" not in campaign.reference(campaign.parse(REAL))


def test_rendering_is_how_a_stray_secret_leaves_the_file():
    """render() deliberately omits dm_only_stray. The caller must have put it
    somewhere first -- but once it has, saving the form is what removes it from
    campaign.md rather than leaving a second copy behind."""
    rendered = campaign.render(campaign.parse(REAL))
    assert "doppelganger" not in rendered
    assert "DM only" not in rendered


# ------------------------------------------------------------- properties ---

CORPUS = [
    "", "   ", "\n\n\n", REAL,
    "no headings at all, just prose",
    "## Where we are",                       # heading with no body
    "##Where we are\nnot a heading (no space)",
    "#### too deep\n\n## Where we are\nHere.",
    "## DM only\nsecrets\n## DM only\nmore secrets",
    "Preamble prose.\n\n## Where we are\nHere.",
    "\x00\x01 binary-ish �\n## Setting\nA place.",
    "## Where we are\r\nWindows line endings.\r\n",
    "- a bullet at the top level\n## House rules\n- one",
]


@pytest.mark.parametrize("text", CORPUS)
def test_parse_never_raises(text):
    assert isinstance(campaign.parse(text), dict)


@pytest.mark.parametrize("text", CORPUS)
def test_parse_never_loses_a_line(text):
    """Every non-heading, non-boilerplate line of the input survives into some
    field. This is the property that kills a 'helpful' strip or normalise --
    the kind of change that looks like tidying and is actually deleting
    somebody's session notes."""
    blob = "\n".join(campaign.parse(text).values())
    for line in text.replace("\r\n", "\n").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ">")):
            continue
        assert stripped in blob, f"lost: {line!r}"


@pytest.mark.parametrize("text", CORPUS)
def test_the_four_fields_round_trip(text):
    """parse(render(f)) == f on the editable fields. dm_only_stray is excluded
    because render() is *supposed* to drop it -- see the test above."""
    once = campaign.parse(text)
    twice = campaign.parse(campaign.render(once))
    for key in campaign.FIELD_KEYS:
        assert twice[key] == once[key], key


@pytest.mark.parametrize("text", CORPUS)
def test_the_rendered_preamble_does_not_accumulate(text):
    """render() writes boilerplate and parse() drops it. If it did not, every
    save would add another copy of the header to the DM's file."""
    once = campaign.render(campaign.parse(text))
    twice = campaign.render(campaign.parse(once))
    assert twice.count("# Campaign notes") == 1


def test_headingless_prose_survives_into_a_field():
    """A table whose notes are one paragraph with no headings at all -- which
    is what a new DM writes -- must still reach the model."""
    fields = campaign.parse("The party are in a mine. It is raining.")
    assert "The party are in a mine." in fields["other"]
    assert "The party are in a mine." in campaign.reference(fields)


def test_prose_before_the_first_heading_is_not_lost():
    fields = campaign.parse("Some stray thought.\n\n## Where we are\nHere.")
    assert "Some stray thought." in fields["other"]
    assert fields["where"] == "Here."


def test_empty_fields_render_no_empty_headings():
    """A file of bare headings reads like something went wrong."""
    rendered = campaign.render({"where": "Here.", "setting": "", "other": ""})
    assert "## Where we are" in rendered
    assert "## Setting" not in rendered


def test_is_empty_ignores_a_quarantined_secret():
    """A table whose only content is a stray DM-only section has no campaign
    notes -- and must not be treated as though it had."""
    assert campaign.is_empty(campaign.parse("## DM only\nsecrets"))
