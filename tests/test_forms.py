"""The fifteen find familiar drawings (static/forms/)."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORMS = ROOT / "static" / "forms"

# The order the spell lists them in: "bat, cat, crab, frog (toad), hawk,
# lizard, octopus, owl, poisonous snake, fish (quipper), rat, raven, sea horse,
# spider, or weasel". Names are the file stems in static/forms/.
SPELL_FORMS = [
    "bat", "cat", "crab", "frog", "hawk", "lizard", "octopus", "owl",
    "poisonous-snake", "fish", "rat", "raven", "sea-horse", "spider",
    "weasel",
]


def test_every_form_the_spell_allows_is_here():
    assert set(SPELL_FORMS) == {p.stem for p in FORMS.glob("*.svg")}


@pytest.mark.parametrize("stem", SPELL_FORMS)
def test_each_drawing_is_a_64_square_in_currentcolor(stem):
    """The two things a caller is entitled to assume: it fits a 64x64 box, and
    it takes the colour of whatever it is put in. A hardcoded stroke would
    paint itself grey on somebody else's palette."""
    text = (FORMS / f"{stem}.svg").read_text()
    assert 'viewBox="0 0 64 64"' in text, stem
    assert "stroke: currentColor" in text, stem
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", text), f"{stem} has a raw colour"


@pytest.mark.parametrize("stem", SPELL_FORMS)
def test_each_drawing_names_itself(stem):
    """It is a picture of an animal; something has to say which animal to
    anyone who cannot see it."""
    text = (FORMS / f"{stem}.svg").read_text()
    title = re.search(r"<title[^>]*>([^<]+)</title>", text)
    assert title, f"{stem} has no <title>"
    # "Frog (toad)" and "Fish (quipper)" are the spell's own double names: the
    # filename is the word the spell leads with, the parenthetical rides along
    # in the title so the other one is still findable.
    named = title.group(1).split(" (")[0].lower().replace(" ", "-")
    assert named == stem, f"{stem}.svg calls itself {title.group(1)!r}"
    assert 'role="img"' in text, stem


@pytest.mark.parametrize("svg", [f"forms/{s}.svg" for s in SPELL_FORMS])
def test_every_drawing_is_well_formed_xml(svg):
    """SVG is XML, and a browser that cannot parse it does not tell you --
    a double hyphen inside a comment is enough for Chrome to throw the whole
    file away, with a 200 and nothing logged."""
    ET.parse(ROOT / "static" / svg)
