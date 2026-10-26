"""The shipped example familiars (examples/familiars/)."""

from pathlib import Path

import pytest

from familiars import Familiars

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
SHELLS = ROOT / "static" / "shells"


@pytest.mark.parametrize("fid,shell", [("whiskers", "cat"), ("pellinore", "owl")])
def test_each_example_loads_as_a_familiar(fid, shell):
    """Copying the folder into a table must give a working familiar, wearing a
    shell that exists."""
    fam = Familiars(EXAMPLES, ROOT).get(fid)
    assert fam is not None, f"{fid}/familiar.json did not load"
    assert fam.shell == shell and (SHELLS / shell).is_dir()
    assert fam.name.lower() in fam.wake_words
    assert fam.name in fam.persona_path.read_text(encoding="utf-8")
