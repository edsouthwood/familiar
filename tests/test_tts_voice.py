"""Voice selection is per-call, not process state.

The thing being pinned: two familiars speaking in two voices must not switch
each other's. Before this, the live voice was a module global plus a single
voices/.active file, so /api/voice from one table would have re-voiced every
other table on the box.

These tests never load a real TTS model -- they exercise resolution, the
provider chain and the plumbing, which is where the shared state lived.
"""

import pytest

import brain
import server
from familiars import Familiars


# ------------------------------------------------------------- resolution ---

@pytest.fixture
def voices(tmp_path, monkeypatch):
    d = tmp_path / "voices"
    d.mkdir()
    (d / "pip.wav").write_bytes(b"")
    (d / "grix.safetensors").write_bytes(b"")
    (d / "notes.txt").write_text("not a voice", encoding="utf-8")
    monkeypatch.setattr(brain, "VOICES_DIR", d)
    return d


def test_list_voices_ignores_non_voice_files(voices):
    assert brain.list_pocket_voices() == ["grix", "pip"]


def test_voice_exists(voices):
    assert brain.voice_exists("pip")
    assert brain.voice_exists("grix")
    assert not brain.voice_exists("notes")
    assert not brain.voice_exists("")


@pytest.mark.parametrize("stem,suffix", [("pip", ".wav"), ("grix", ".safetensors")])
def test_resolve_bare_stem_finds_the_file(voices, stem, suffix):
    arg, name = brain._resolve_pocket_voice(stem)
    assert arg == str(voices / f"{stem}{suffix}")
    assert name == stem


def test_resolve_unknown_stem_is_passed_through_as_a_preset(voices):
    """pocket-tts ships preset voices (alba, anna, ...) that aren't files."""
    assert brain._resolve_pocket_voice("alba") == ("alba", "alba")


def test_resolve_relative_path(voices, monkeypatch):
    arg, name = brain._resolve_pocket_voice("voices/pip.wav")
    assert arg == str(voices / "pip.wav")
    assert name == "pip"


def test_resolve_missing_path_raises(voices):
    with pytest.raises(RuntimeError):
        brain._resolve_pocket_voice("voices/nope.wav")


def test_resolve_empty_falls_back_to_environment(voices, monkeypatch):
    monkeypatch.setenv("POCKET_VOICE", "grix")
    assert brain._resolve_pocket_voice("")[1] == "grix"


def test_resolve_name_never_raises_for_display(voices):
    assert brain.resolve_voice_name("pip") == "pip"
    assert brain.resolve_voice_name("voices/nope.wav") == ""


# ---------------------------------------------------- provider plumbing -----

def test_synthesize_speech_passes_the_voice_to_the_provider(monkeypatch):
    seen = []

    def fake(text, voice):
        seen.append((text, voice))
        return b"audio", "audio/wav"

    monkeypatch.setattr(brain, "_TTS_PROVIDERS", {"fake": fake})
    monkeypatch.setenv("TTS_PROVIDER", "fake")

    assert brain.synthesize_speech("hello", "pip") == (b"audio", "audio/wav")
    assert brain.synthesize_speech("hello", "grix") == (b"audio", "audio/wav")
    assert seen == [("hello", "pip"), ("hello", "grix")]


def test_two_voices_in_a_row_do_not_disturb_each_other(monkeypatch):
    """The regression this whole refactor exists to prevent."""
    monkeypatch.setattr(brain, "_TTS_PROVIDERS", {"echo": lambda text, voice: (voice.encode(), "audio/wav")})
    monkeypatch.setenv("TTS_PROVIDER", "echo")

    first, _ = brain.synthesize_speech("x", "pip")
    second, _ = brain.synthesize_speech("x", "grix")
    third, _ = brain.synthesize_speech("x", "pip")
    assert (first, second, third) == (b"pip", b"grix", b"pip")


def test_failing_provider_falls_through_to_the_next(monkeypatch):
    def boom(text, voice):
        raise RuntimeError("no model")

    monkeypatch.setattr(brain, "_TTS_PROVIDERS",
                        {"broken": boom, "ok": lambda t, v: (b"fallback", "audio/wav")})
    monkeypatch.setenv("TTS_PROVIDER", "broken,ok")
    assert brain.synthesize_speech("x", "pip") == (b"fallback", "audio/wav")


def test_no_provider_configured_returns_none(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "")
    assert brain.synthesize_speech("x", "pip") is None


# ------------------------------------------------------------- endpoints ----

@pytest.fixture
def bound(live, voices):
    """A signed-in DM at a table with two familiars, Pip bound.

    These are the endpoint tests, so they go through a real request with a real
    cookie: `/api/voice` is DM-only now, and a test that bypassed the decorator
    would not notice if that stopped being true.
    """
    fams = live.table.familiars
    fams.create("Pip", voice="pip")
    fams.create("Grix", voice="grix")
    fams.set_active("pip")
    live.fams = fams
    return live


def test_api_voices_reports_this_familiars_voice(bound):
    body = bound.client.get(bound.url("/voices")).get_json()
    assert body["voices"] == ["grix", "pip"]
    assert body["current"] == "pip"

    bound.fams.set_active("grix")
    assert bound.client.get(bound.url("/voices")).get_json()["current"] == "grix"


def test_api_voice_writes_to_the_bound_familiar_only(bound):
    r = bound.client.post(bound.url("/voice"), json={"voice": "grix"})
    assert r.status_code == 200
    assert r.get_json() == {"voice": "grix"}

    assert bound.fams.get("pip").voice == "grix", "the bound familiar changed"
    assert bound.fams.get("grix").voice == "grix", "the other familiar is untouched"


def test_api_voice_rejects_unknown_voice(bound):
    r = bound.client.post(bound.url("/voice"), json={"voice": "nope"})
    assert r.status_code == 400
    assert bound.fams.get("pip").voice == "pip"


def test_api_voice_rejects_empty(bound):
    assert bound.client.post(bound.url("/voice"), json={}).status_code == 400


def test_api_tts_speaks_in_the_bound_familiars_voice(bound, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "synthesize_speech",
                        lambda text, voice, **kw: seen.append(voice) or (b"wav", "audio/wav"))

    bound.client.post(bound.url("/tts"), json={"text": "hello"})
    bound.fams.set_active("grix")
    bound.client.post(bound.url("/tts"), json={"text": "hello"})

    assert seen == ["pip", "grix"]


def test_a_players_voice_change_is_refused(bound):
    """Choosing the familiar's voice is the DM's call. A player asking is not
    an error worth hiding, but it must not take effect."""
    player = bound.player_client()
    r = player.post(bound.url("/voice"), json={"voice": "grix"})
    assert r.status_code == 403
    assert bound.fams.get("pip").voice == "pip"
