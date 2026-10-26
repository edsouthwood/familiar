"""Metering and the cost readout.

These tests care most about two things: that a call which spends money is
recorded against the right table, and that the numbers which decide the price
(the model, and the cache hit rate) are reported honestly.
"""

import pytest

import brain
import server
import usage as usage_policy
from tenancy import TableRuntime


# ------------------------------------------------------------ the prompt ----

def test_the_cache_breakpoint_sits_between_stable_and_volatile():
    """Caching is a prefix match, so the breakpoint has to fall *after* the
    persona and campaign and *before* the memory a fold-in rewrites. With the
    old single block, every fold-in threw the whole cache away."""
    sp = brain.SystemPrompt(stable="who I am", volatile="what just happened")
    blocks = brain._claude_system(sp)

    assert blocks[0]["text"] == "who I am"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"] == "what just happened"
    assert "cache_control" not in blocks[1], "a breakpoint here re-caches every fold-in"


def test_a_prompt_with_no_volatile_half_is_one_block():
    blocks = brain._claude_system(brain.SystemPrompt(stable="just this"))
    assert len(blocks) == 1


def test_build_system_prompt_puts_the_rewritten_files_in_the_volatile_half(make_table):
    table, _rt = make_table()
    fams = table.familiars
    fams.create("Pip", persona="PERSONA-TEXT")
    fams.set_active("pip")
    fams.write_bond(fams.active(), "BOND-TEXT")
    (table.dir / "campaign.md").write_text("CAMPAIGN-TEXT", encoding="utf-8")
    table.game_context_file.write_text("MEMORY-TEXT", encoding="utf-8")

    sp = server.build_system_prompt(table)

    assert "PERSONA-TEXT" in sp.stable and "CAMPAIGN-TEXT" in sp.stable
    assert "BOND-TEXT" in sp.volatile and "MEMORY-TEXT" in sp.volatile
    # Both halves still reach the model; only the caching changed.
    assert all(x in sp.text for x in ("PERSONA-TEXT", "CAMPAIGN-TEXT", "BOND-TEXT", "MEMORY-TEXT"))


def test_a_table_with_no_notes_contributes_no_campaign_text(make_table):
    """A brand-new table used to be handed campaign.example.md as though it
    were campaign fact -- so the familiar was told "Name each player character:
    their class, a trait or two" and treated it as something it had witnessed.
    The DM meanwhile saw a blank box, because the editor read the same file
    optionally. Silence is the honest answer for a table with no notes."""
    table, _rt = make_table()
    assert not (table.dir / "campaign.md").exists()
    # Assert against the fallback file's *actual* content, whatever it is --
    # naming a phrase from the real repo's campaign.example.md would pass here
    # for the wrong reason, because the fixture ships its own.
    fallback = (table.app_root / "campaign.example.md").read_text().strip()
    assert fallback, "the fixture must have a fallback for this test to mean anything"

    sp = server.build_system_prompt(table)

    assert fallback not in sp.text
    # The familiar itself still falls back -- a creature with no persona has
    # nothing to be, which is not the same as a table with no notes.
    assert (table.app_root / "persona.example.md").read_text().strip() in sp.text


def test_a_table_with_no_notes_folds_in_no_campaign_text(make_table):
    """The same, on the other path. The fold-in reference must not put the
    example file's instructions to the DM into the table's memory as though
    the room had said them."""
    table, _rt = make_table()
    fallback = (table.app_root / "campaign.example.md").read_text().strip()
    assert fallback not in server._campaign_reference(table)


def test_a_plain_string_is_treated_as_entirely_stable():
    """tools/ and older callers pass a bare string."""
    sp = brain._as_system("just a string")
    assert sp.stable == "just a string" and sp.volatile == ""


# ------------------------------------------------------------- providers ----

def test_gemini_usage_separates_cached_from_fresh_input():
    """promptTokenCount includes the cached part; pricing them the same would
    overstate cost by up to 10x on a well-cached prompt."""
    u = brain._gemini_usage(
        {"usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 50,
                           "cachedContentTokenCount": 800}}, "gemini-2.5-flash")
    assert (u.input_tokens, u.cache_read, u.output_tokens) == (200, 800, 50)


def test_gemini_usage_survives_a_response_with_no_metadata():
    u = brain._gemini_usage({}, "gemini-2.5-flash")
    assert u.input_tokens == 0 and u.provider == "gemini"


def test_gemini_usage_never_goes_negative():
    u = brain._gemini_usage(
        {"usageMetadata": {"promptTokenCount": 100, "cachedContentTokenCount": 900}}, "m")
    assert u.input_tokens == 0


def test_claude_usage_reads_both_cache_counters():
    class U:
        input_tokens, output_tokens = 120, 40
        cache_read_input_tokens, cache_creation_input_tokens = 900, 30

    class R:
        usage = U()

    u = brain._claude_usage(R(), "claude-haiku-4-5")
    assert (u.input_tokens, u.output_tokens, u.cache_read, u.cache_write) == (120, 40, 900, 30)


def test_the_default_claude_model_is_the_cheap_one():
    """A default nobody chose is a bad place for a 5x cost multiplier to hide."""
    assert brain.DEFAULT_CLAUDE_MODEL == "claude-haiku-4-5"


def test_the_callers_model_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-8")
    assert brain._claude_model(brain.LLMOptions(model="claude-sonnet-5")) == "claude-sonnet-5"


def test_a_failing_usage_callback_cannot_break_a_call():
    def explode(_u):
        raise RuntimeError("bookkeeping is down")

    brain.LLMOptions(on_usage=explode).report(brain.LLMUsage(provider="x"))  # must not raise


# ------------------------------------------------------------- pricing ----

def test_the_priced_model_follows_the_provider_chain(monkeypatch):
    """Gemini tokens priced at Claude rates would be a made-up number."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert usage_policy.configured_model() == "gemini-2.5-flash"
    monkeypatch.setenv("LLM_PROVIDER", "local,claude")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5")
    assert usage_policy.configured_model() == "claude-sonnet-5"


def test_caching_changes_the_bill_by_the_expected_order():
    fresh = usage_policy.estimate_cost(
        {"llm_tokens_in": 1_000_000, "llm_tokens_out": 20_000}, "claude-haiku-4-5")
    cached = usage_policy.estimate_cost(
        {"llm_tokens_in": 200_000, "cache_read_tokens": 800_000, "llm_tokens_out": 20_000},
        "claude-haiku-4-5")
    assert cached["usd"] < fresh["usd"] / 2


def test_an_unpriced_provider_returns_no_estimate_rather_than_a_guess():
    """A made-up number in a cost model is worse than a blank: it looks like
    evidence."""
    assert usage_policy.estimate_cost({"llm_tokens_in": 1_000}, "gemini-2.5-flash") is None


def test_cache_hit_rate_is_none_before_anything_is_recorded():
    assert usage_policy.cache_hit_rate({}) is None


def test_cache_hit_rate_reports_zero_when_caching_is_not_engaging():
    """The failure this number exists to catch: a prompt under the ~1024-token
    minimum caches silently -- no error, no discount."""
    assert usage_policy.cache_hit_rate({"llm_tokens_in": 5000, "cache_read_tokens": 0}) == 0.0


# --------------------------------------------------------------- metering ---

def test_a_question_is_metered_against_its_own_table(live, monkeypatch):
    live.table.familiars.create("Pip", persona="You are Pip.")
    monkeypatch.setattr(server, "ask_llm", _fake_llm(input_tokens=500, output_tokens=60))

    live.client.post(live.url("/ask"), json={"question": "who goes there?"})

    totals = live.db.usage_totals(live.table.id)
    assert totals["requests"] == 1
    assert totals["llm_tokens_in"] == 500
    assert totals["llm_tokens_out"] == 60


def test_the_model_comes_from_the_environment_not_the_table(live, monkeypatch):
    """No per-table model: an empty choice lets brain read CLAUDE_MODEL or
    GEMINI_MODEL."""
    live.table.familiars.create("Pip", persona="You are Pip.")
    seen = {}

    def fake(system, question, history, **kw):
        seen.update(kw)
        return "answered"

    monkeypatch.setattr(server, "ask_llm", fake)
    live.client.post(live.url("/ask"), json={"question": "hello"})
    assert seen["model"] == ""


def test_metering_failure_does_not_break_answering(live, monkeypatch):
    live.table.familiars.create("Pip", persona="You are Pip.")
    monkeypatch.setattr(server, "ask_llm", _fake_llm(input_tokens=10))
    monkeypatch.setattr(live.db, "record_usage",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))

    r = live.client.post(live.url("/ask"), json={"question": "still there?"})
    assert r.status_code == 200 and r.get_json()["answer"] == "answered"


def test_tts_is_metered_in_characters(live, monkeypatch):
    live.table.familiars.create("Pip")
    monkeypatch.setenv("TTS_PROVIDER", "fake")
    monkeypatch.setitem(brain._TTS_PROVIDERS, "fake", lambda text, voice: (b"wav", "audio/wav"))

    live.client.post(live.url("/tts"), json={"text": "twelve chars"})

    assert live.db.usage_totals(live.table.id)["tts_chars"] == len("twelve chars")


def test_usage_does_not_cross_between_tables(live, monkeypatch):
    other_user = live.db.create_user("o@example.com", "another-long-password")
    other = live.db.create_table(other_user, "Blackmoor", "blackmoor")
    live.table.familiars.create("Pip", persona="You are Pip.")
    monkeypatch.setattr(server, "ask_llm", _fake_llm(input_tokens=300))

    live.client.post(live.url("/ask"), json={"question": "hello"})

    assert live.db.usage_totals(live.table.id)["llm_tokens_in"] == 300
    assert live.db.usage_totals(other)["llm_tokens_in"] == 0


# ------------------------------------------------------- ambient braking ----

def test_the_fold_in_rate_cap_holds_at_the_limit():
    rt = TableRuntime("t")
    assert [rt.allow_fold_in(3) for _ in range(5)] == [True, True, True, False, False]


def test_a_refused_fold_in_does_not_extend_its_own_window():
    """Otherwise a stuck recorder would keep pushing the hour forward and lock
    the table out indefinitely."""
    rt = TableRuntime("t")
    for _ in range(50):
        rt.allow_fold_in(2)
    assert len(rt.fold_in_times) == 2


def test_ambient_over_the_rate_cap_drops_the_chunk_without_erroring(live, monkeypatch):
    """The browser does not await /api/ambient, so refusing with an error would
    be invisible. Dropping the transcript is the honest failure."""
    live.table.familiars.create("Pip", persona="You are Pip.")
    folds = []
    monkeypatch.setattr(server, "_summarize_ambient", lambda *a: folds.append(1))
    monkeypatch.setattr(usage_policy, "FOLD_INS_PER_HOUR", 1)

    chunk = {"text": " ".join(["word"] * (server.AMBIENT_WORD_THRESHOLD + 1))}
    first = live.client.post(live.url("/ambient"), json=chunk)
    second = live.client.post(live.url("/ambient"), json=chunk)

    assert first.status_code == second.status_code == 200
    assert len(folds) == 1, "the second chunk should have been dropped, not folded in"


# -------------------------------------------------------------- endpoint ----

def test_the_usage_endpoint_reports_what_was_spent(live, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    live.db.record_usage(live.table.id, "claude", requests=3,
                         llm_tokens_in=1000, cache_read_tokens=3000, llm_tokens_out=200)
    body = live.client.get(live.url("/usage")).get_json()

    assert body["totals"]["llm_tokens_in"] == 1000
    assert body["cache_hit_rate"] == 0.75
    assert body["model"] == "claude-haiku-4-5"
    assert body["estimated_cost"]["usd"] > 0


def test_a_month_on_free_gemini_is_not_priced(live, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    live.db.record_usage(live.table.id, "gemini", requests=3, llm_tokens_in=1000)
    assert live.client.get(live.url("/usage")).get_json()["estimated_cost"] is None


def test_the_usage_endpoint_is_dm_only(live):
    assert live.player_client().get(live.url("/usage")).status_code == 403


def _fake_llm(input_tokens=0, output_tokens=0, provider="claude"):
    """Stand in for ask_llm, reporting usage the way a real provider would."""
    def fake(system, question, history, model="", on_usage=None, reply=False):
        if on_usage is not None:
            on_usage(brain.LLMUsage(provider=provider, model=model,
                                    input_tokens=input_tokens,
                                    output_tokens=output_tokens))
        return "answered"
    return fake


# ------------------------------------------------- unmeasured providers ----

def test_a_month_on_the_claude_cli_reports_no_cost_rather_than_zero(live):
    """The CLI answers from the subscription on the machine and reports no
    tokens. "$0.00" would read as "this month was free"; it means "nobody
    counted"."""
    live.db.record_usage(live.table.id, "claude-cli", requests=7)
    body = live.client.get(live.url("/usage")).get_json()

    assert body["estimated_cost"] is None
    assert body["unmetered"] == ["claude-cli"]
    assert body["totals"]["requests"] == 7


def test_a_metered_provider_is_not_flagged_as_unmeasured(live, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    live.db.record_usage(live.table.id, "claude", requests=2, llm_tokens_in=900)
    body = live.client.get(live.url("/usage")).get_json()

    assert body["unmetered"] == []
    assert body["estimated_cost"]["usd"] > 0


def test_a_mixed_month_still_prices_what_it_can(live, monkeypatch):
    """Some answers from the CLI, some from a metered key."""
    monkeypatch.setenv("LLM_PROVIDER", "claude-cli,claude")
    live.db.record_usage(live.table.id, "claude-cli", requests=5)
    live.db.record_usage(live.table.id, "claude", requests=2, llm_tokens_in=900)
    body = live.client.get(live.url("/usage")).get_json()

    assert body["unmetered"] == ["claude-cli"]
    assert body["estimated_cost"]["usd"] > 0, "the metered half is still priceable"
