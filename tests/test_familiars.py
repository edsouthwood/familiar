"""Familiar bundle loading, binding and creation."""

import json

import pytest

from familiars import DEFAULT_LANG, DEFAULT_SHELL, Familiars, slugify_id


@pytest.fixture
def fams(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "persona.example.md").write_text("A default familiar.", encoding="utf-8")
    return Familiars(tmp_path / "data", tmp_path / "app")


def _make(fams, familiar_id, **doc):
    d = fams.dir / familiar_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "familiar.json").write_text(json.dumps(doc), encoding="utf-8")
    return d


# ------------------------------------------------------------------ slugify_id

@pytest.mark.parametrize(
    "name,expected",
    [("Whiskers", "whiskers"), ("Old Man Grix", "old-man-grix"), ("  Pip!  ", "pip"), ("", "familiar")],
)
def test_slugify_id(name, expected):
    assert slugify_id(name) == expected


# ---------------------------------------------------------------------- list

def test_list_empty_when_no_directory(fams):
    assert fams.list() == []
    assert fams.active() is None


def test_list_is_ordered_by_name(fams):
    _make(fams, "zeta", name="Aardvark")
    _make(fams, "alpha", name="Zebra")
    assert [f.name for f in fams.list()] == ["Aardvark", "Zebra"]


def test_malformed_familiar_is_skipped_not_raised(fams):
    _make(fams, "good", name="Good")
    bad = fams.dir / "bad"
    bad.mkdir(parents=True)
    (bad / "familiar.json").write_text("{ not json", encoding="utf-8")
    assert [f.id for f in fams.list()] == ["good"]


def test_directory_without_familiar_json_is_ignored(fams):
    _make(fams, "real", name="Real")
    (fams.dir / "stray").mkdir(parents=True)
    assert [f.id for f in fams.list()] == ["real"]


# ----------------------------------------------------------------- defaults

def test_defaults_are_filled_in(fams):
    _make(fams, "pip", name="Pip")
    f = fams.get("pip")
    assert f.lang == DEFAULT_LANG
    assert f.shell == DEFAULT_SHELL
    assert f.voice == ""


def test_wake_words_are_lowercased_and_blanks_dropped(fams):
    _make(fams, "pip", name="Pip", wakeWords=["Pip", "  PIPPIN ", "", "   "])
    assert fams.get("pip").wake_words == ["pip", "pippin"]


def test_public_falls_back_to_name_when_no_wake_words(fams):
    _make(fams, "pip", name="Pip")
    assert fams.get("pip").public()["wakeVariants"] == ["pip"]


def test_public_shape(fams):
    _make(fams, "pip", name="Pip", wakeWords=["pip"], lang="en-US", shell="owl", sounds="owl", voice="secret.wav")
    doc = fams.get("pip").public()
    assert doc == {"id": "pip", "name": "Pip", "wakeVariants": ["pip"], "lang": "en-US",
                   "shell": "owl", "sounds": "owl", "owner": ""}
    assert "voice" not in doc, "the voice file path is server-side detail, not browser identity"


# ------------------------------------------------------------------ get/guard

@pytest.mark.parametrize("bad", ["..", "../etc", "/etc/passwd", "Pip", "", "a" * 65, "-pip"])
def test_get_rejects_non_slug_ids(fams, bad):
    _make(fams, "pip", name="Pip")
    assert fams.get(bad) is None


def test_get_missing_is_none(fams):
    assert fams.get("nobody") is None


# --------------------------------------------------------------------- active

def test_active_falls_back_to_first_when_unset(fams):
    _make(fams, "zeta", name="Aardvark")
    _make(fams, "alpha", name="Zebra")
    assert fams.active().name == "Aardvark"


def test_set_active_then_active_round_trips(fams):
    _make(fams, "one", name="One")
    _make(fams, "two", name="Two")
    fams.set_active("two")
    assert fams.active().id == "two"


def test_active_falls_back_when_pointer_dangles(fams):
    _make(fams, "one", name="One")
    fams.dir.mkdir(parents=True, exist_ok=True)
    (fams.dir / ".active").write_text("deleted-familiar\n", encoding="utf-8")
    assert fams.active().id == "one"


def test_set_active_rejects_unknown(fams):
    _make(fams, "one", name="One")
    with pytest.raises(ValueError):
        fams.set_active("nope")


def test_active_pointer_is_not_listed_as_a_familiar(fams):
    _make(fams, "one", name="One")
    fams.set_active("one")
    assert [f.id for f in fams.list()] == ["one"]


# --------------------------------------------------------------------- create

def test_create_derives_id_from_name(fams):
    f = fams.create("Old Man Grix")
    assert f.id == "old-man-grix"
    assert f.name == "Old Man Grix"
    assert fams.get("old-man-grix") is not None


def test_create_is_idempotent_and_does_not_overwrite(fams):
    fams.create("Pip", persona="original")
    again = fams.create("Pip", persona="REPLACED")
    assert again.id == "pip"
    assert fams.persona_text(again) == "original"


def test_create_writes_persona(fams):
    f = fams.create("Pip", persona="A small brave thing.")
    assert fams.persona_text(f) == "A small brave thing."


def test_create_accepts_explicit_id_and_fields(fams):
    f = fams.create("Whiskers", id="whiskers", wake_words=["Whiskers", "WHISKER"],
                    lang="en-GB", shell="cat", sounds="cat", voice="voices/fem.wav")
    assert f.id == "whiskers"
    assert f.wake_words == ["whiskers", "whisker"]
    assert f.voice == "voices/fem.wav"


def test_create_rejects_bad_explicit_id(fams):
    with pytest.raises(ValueError):
        fams.create("Pip", id="../escape")


# ------------------------------------------------------------- persona / bond

def test_persona_falls_back_to_app_example(fams):
    f = fams.create("Pip")  # no persona written
    assert fams.persona_text(f) == "A default familiar."


def test_persona_falls_back_with_no_familiar_at_all(fams):
    assert fams.persona_text(None) == "A default familiar."


def test_bond_absent_is_empty_not_an_error(fams):
    assert fams.bond_text(fams.create("Pip")) == ""


def test_bond_round_trips(fams):
    f = fams.create("Pip")
    fams.write_bond(f, "  The party trusts Pip.  ")
    assert fams.bond_text(f).strip() == "The party trusts Pip."


def test_write_bond_with_no_familiar_is_a_noop(fams):
    fams.write_bond(None, "nowhere")  # must not raise
