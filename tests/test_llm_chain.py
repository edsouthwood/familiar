"""LLM_PROVIDER_REPLY reroutes spoken answers only.

The point of the split: a player waiting on an answer is worth a fast metered
API, an ambient fold-in nobody is waiting on is not. So the override must
reach ask_llm/ask_llm_stream when -- and only when -- the caller says this is
a reply.
"""

import brain


def _record(monkeypatch, name, stream=False):
    """Register a fake provider that notes it was called."""
    calls = []

    if stream:
        def provider(system, question, history, opts):
            calls.append(name)
            yield "ok"
        monkeypatch.setitem(brain._LLM_STREAM_PROVIDERS, name, provider)
    else:
        def provider(system, question, history, opts):
            calls.append(name)
            return "ok"
        monkeypatch.setitem(brain._LLM_PROVIDERS, name, provider)
    return calls


def test_reply_chain_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "local,gemini")
    monkeypatch.delenv("LLM_PROVIDER_REPLY", raising=False)
    assert brain._llm_chain(reply=True) == ["local", "gemini"]


def test_reply_chain_overrides_only_replies(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude-cli,gemini")
    monkeypatch.setenv("LLM_PROVIDER_REPLY", "claude,claude-cli")
    assert brain._llm_chain(reply=True) == ["claude", "claude-cli"]
    assert brain._llm_chain() == ["claude-cli", "gemini"]


def test_blank_reply_chain_is_ignored(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_PROVIDER_REPLY", "  ")
    assert brain._llm_chain(reply=True) == ["gemini"]


def test_ask_llm_routes_by_purpose(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake-free")
    monkeypatch.setenv("LLM_PROVIDER_REPLY", "fake-fast")
    free = _record(monkeypatch, "fake-free")
    fast = _record(monkeypatch, "fake-fast")
    brain.ask_llm("sys", "q", [])
    brain.ask_llm("sys", "q", [], reply=True)
    assert free == ["fake-free"] and fast == ["fake-fast"]


def test_ask_llm_stream_routes_by_purpose(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake-free")
    monkeypatch.setenv("LLM_PROVIDER_REPLY", "fake-fast")
    free = _record(monkeypatch, "fake-free", stream=True)
    fast = _record(monkeypatch, "fake-fast", stream=True)
    assert "".join(brain.ask_llm_stream("sys", "q", [], reply=True)) == "ok"
    assert fast == ["fake-fast"] and free == []


def test_cli_never_sees_the_api_key(monkeypatch):
    # The CLI prefers ANTHROPIC_API_KEY over the subscription login, so a key
    # set for reply-path API calls must not leak into the free CLI path.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert "ANTHROPIC_API_KEY" not in brain._claude_cli_env()
