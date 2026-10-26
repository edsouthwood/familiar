"""LLM adapter for the familiar.

One function, two providers. Switch with the LLM_PROVIDER env var:

    LLM_PROVIDER=gemini  (default)  needs GEMINI_API_KEY
    LLM_PROVIDER=claude             needs ANTHROPIC_API_KEY

`history` is a list of (question, answer) tuples from earlier in the session.

Text-to-speech lives here too — see synthesize_speech() near the bottom. The
voice is pluggable: point TTS_PROVIDER at one or more sources and they're tried
in order; if none is configured or all fail, the browser falls back to its own
speechSynthesis voice.
"""

import io
import json
import logging
import os
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import NamedTuple

import requests

# One pooled HTTP session for every outbound call (LLMs and TTS), so we reuse
# the TCP/TLS connection instead of paying a fresh handshake per question.
_session = requests.Session()

# Circuit breaker: a provider that fails to *connect* (e.g. the home-network PC
# is powered off) is skipped until this monotonic deadline, so only one question
# a minute pays its connect timeout instead of every single one.
_LLM_COOLDOWN_S = 60.0
_skip_until: dict[str, float] = {}


def _provider_ready(name: str) -> bool:
    """False while a provider is in its post-connect-failure cooldown."""
    return _skip_until.get(name, 0.0) <= time.monotonic()


def _note_provider_failure(name: str, exc: Exception) -> None:
    """Start a cooldown for `name` if it failed to reach the server at all.
    Only connection/timeout errors self-heal (the box comes back); a real HTTP
    error returns fast and stays in the chain so it surfaces promptly."""
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        _skip_until[name] = time.monotonic() + _LLM_COOLDOWN_S
        logging.info("LLM provider %r unreachable — skipping it for %.0fs", name, _LLM_COOLDOWN_S)


class SystemPrompt(NamedTuple):
    """A system prompt split into its stable and volatile halves.

    Prompt caching is a prefix match: one changed byte anywhere in the prefix
    throws away everything after it. The familiar's persona and the campaign
    notes are written once and then sit still for months; its bond with the
    party and its running memory of the session are rewritten by every ambient
    fold-in, several times an hour. Concatenating them into one string -- which
    is what this used to be -- meant the cacheable half was never cacheable,
    because the volatile half was inside the same block.

    Providers that do not cache just use `.text` and never notice the split.
    """
    stable: str = ""
    volatile: str = ""

    @property
    def text(self) -> str:
        return "\n\n---\n\n".join(p for p in (self.stable, self.volatile) if p)


class LLMUsage(NamedTuple):
    """What one call actually consumed.

    `cache_read` and `cache_write` are separate because they are priced
    differently -- roughly 0.1x and 1.25x an ordinary input token -- so a bill
    cannot be predicted from `input_tokens` alone. They are also the only way
    to tell whether caching is working at all: if `cache_read` stays at zero
    across repeated questions, something ahead of the breakpoint is changing.

    Every field may be zero for a provider that does not report token counts
    (the Claude CLI does not), in which case `requests` is still worth
    recording so the call is not invisible.
    """
    provider: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0


class LLMOptions(NamedTuple):
    """Per-call knobs threaded down to whichever provider answers.

    `model` comes from the table's plan rather than from the environment, so a
    base table is answered by the model it was costed on. `on_usage` is how
    metering gets out without brain.py knowing anything about tables or
    databases -- it is handed a callback and calls it.
    """
    model: str = ""
    on_usage: object = None

    def report(self, usage: LLMUsage) -> None:
        """Hand usage to the callback, never letting metering break answering."""
        if self.on_usage is None:
            return
        try:
            self.on_usage(usage)
        except Exception:
            logging.exception("usage callback failed; continuing")


_NO_OPTIONS = LLMOptions()


def _as_system(system) -> SystemPrompt:
    """Accept a plain string as well as a SystemPrompt.

    The tools in tools/ and every older caller pass a string. Treating it as
    entirely stable is the right reading: a caller that has not thought about
    the split has not put a timestamp in the middle of it either.
    """
    return system if isinstance(system, SystemPrompt) else SystemPrompt(stable=str(system))


def _llm_chain(reply: bool = False) -> list[str]:
    """LLM_PROVIDER is a comma-separated chain tried in order, e.g.
    "local,gemini" = ask the home-network LLM, fall back to Gemini if that
    machine is off. Single names keep working as before.

    LLM_PROVIDER_REPLY, if set, overrides it for answers spoken to a player
    only -- the latency-critical path -- so those can go to a fast metered API
    while ambient fold-ins and recaps stay on the free chain."""
    raw = (reply and os.environ.get("LLM_PROVIDER_REPLY", "").strip()) \
        or os.environ.get("LLM_PROVIDER", "gemini")
    chain = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return chain or ["gemini"]


def ask_llm(system, question: str, history: list[tuple[str, str]],
            model: str = "", on_usage=None, reply: bool = False) -> str:
    system = _as_system(system)
    opts = LLMOptions(model=model, on_usage=on_usage)
    errors = []
    for name in _llm_chain(reply):
        provider = _LLM_PROVIDERS.get(name)
        if provider is None:
            logging.warning("Unknown LLM provider %r — skipping", name)
            continue
        if not _provider_ready(name):
            continue  # in cooldown after a recent connect failure
        try:
            return provider(system, question, history, opts)
        except Exception as exc:
            logging.exception("LLM provider %r failed; trying next", name)
            _note_provider_failure(name, exc)
            errors.append(f"{name}: {exc}")
    raise RuntimeError("All LLM providers failed — " + "; ".join(errors or ["none configured"]))


def ask_llm_stream(system, question: str, history: list[tuple[str, str]],
                   model: str = "", on_usage=None, reply: bool = False):
    """Like ask_llm, but a generator yielding the answer in text deltas.

    Walks the same provider chain, but only falls over to the next provider
    if the current one failed *before yielding any text* — once words are out,
    restarting with another provider would repeat them, so mid-stream errors
    propagate and the client speaks whatever arrived.
    """
    system = _as_system(system)
    opts = LLMOptions(model=model, on_usage=on_usage)
    errors = []
    for name in _llm_chain(reply):
        provider = _LLM_STREAM_PROVIDERS.get(name)
        if provider is None:
            logging.warning("Unknown LLM provider %r — skipping", name)
            continue
        if not _provider_ready(name):
            continue  # in cooldown after a recent connect failure
        yielded = False
        try:
            for delta in provider(system, question, history, opts):
                yielded = True
                yield delta
            return
        except Exception as exc:
            if yielded:
                raise
            logging.exception("LLM provider %r failed before any output; trying next", name)
            _note_provider_failure(name, exc)
            errors.append(f"{name}: {exc}")
    raise RuntimeError("All LLM providers failed — " + "; ".join(errors or ["none configured"]))


def _gemini_key_and_model() -> tuple[str, str]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey"
        )
    return api_key, os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def _gemini_contents(question: str, history: list[tuple[str, str]]) -> list:
    contents = []
    for q, a in history:
        contents.append({"role": "user", "parts": [{"text": q}]})
        contents.append({"role": "model", "parts": [{"text": a}]})
    contents.append({"role": "user", "parts": [{"text": question}]})
    return contents


def _gemini_generation_config() -> dict:
    config = {"maxOutputTokens": 800}
    # Gemini 2.5 Flash is a thinking model, and nearly all of its latency is
    # the (invisible) thinking phase — ~3s vs ~0.4s to the first spoken-word.
    # Off by default for table snappiness: 0 = off, -1 = Gemini decides,
    # N = token budget for thinking.
    budget = os.environ.get("GEMINI_THINKING_BUDGET", "0")
    try:
        config["thinkingConfig"] = {"thinkingBudget": int(budget)}
    except ValueError:
        logging.warning("Ignoring non-integer GEMINI_THINKING_BUDGET %r", budget)
    return config


def _gemini_usage(data: dict, model: str) -> LLMUsage:
    """Read Gemini's usageMetadata. Absent fields are zero, not an error --
    metering must never be the thing that breaks a working answer."""
    meta = data.get("usageMetadata") or {}
    cached = int(meta.get("cachedContentTokenCount") or 0)
    return LLMUsage(
        provider="gemini",
        model=model,
        # promptTokenCount includes the cached part, so subtract it to keep
        # "fresh input" and "cache read" priced separately, as Anthropic
        # reports them. Clamped: a provider that changes its mind about what
        # promptTokenCount includes should not produce negative usage.
        input_tokens=max(0, int(meta.get("promptTokenCount") or 0) - cached),
        output_tokens=int(meta.get("candidatesTokenCount") or 0),
        cache_read=cached,
    )


def _ask_gemini(system, question: str, history: list[tuple[str, str]],
                opts=_NO_OPTIONS) -> str:
    api_key, model = _gemini_key_and_model()
    resp = _session.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key},
        json={
            "system_instruction": {"parts": [{"text": system.text}]},
            "contents": _gemini_contents(question, history),
            "generationConfig": _gemini_generation_config(),
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    opts.report(_gemini_usage(data, model))
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        raise RuntimeError(f"Unexpected Gemini response: {data}")


def _ask_gemini_stream(system, question: str, history: list[tuple[str, str]],
                       opts=_NO_OPTIONS):
    api_key, model = _gemini_key_and_model()
    resp = _session.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse",
        headers={"x-goog-api-key": api_key},
        json={
            "system_instruction": {"parts": [{"text": system.text}]},
            "contents": _gemini_contents(question, history),
            "generationConfig": _gemini_generation_config(),
        },
        stream=True,
        timeout=60,
    )
    resp.raise_for_status()
    last = None
    for line in resp.iter_lines():
        if not line.startswith(b"data: "):
            continue
        data = json.loads(line[6:])
        # Every event carries a running usageMetadata; the last one is the
        # total. Keeping the most recent means the count survives even if the
        # stream ends without a distinct final event.
        if data.get("usageMetadata"):
            last = data
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            continue  # e.g. the final usage-metadata event, which carries no text
        if text:
            yield text
    if last is not None:
        opts.report(_gemini_usage(last, model))


DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5"


def _claude_model(opts) -> str:
    """Which model answers: the caller's choice if it made one, else
    CLAUDE_MODEL, else a cheap default -- a default nobody chose is a bad place
    for a 5x cost multiplier to hide.
    """
    return opts.model or os.environ.get("CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL


def _claude_messages(question: str, history: list[tuple[str, str]]) -> list:
    messages = []
    for q, a in history:
        messages.append({"role": "user", "content": q})
        messages.append({"role": "assistant", "content": a})
    messages.append({"role": "user", "content": question})
    return messages


def _claude_system(system) -> list:
    """The system prompt as cache-aware blocks.

    `cache_control` goes on the stable block only. Everything after a
    breakpoint is re-read every time, so putting the breakpoint after the
    persona and campaign notes -- and leaving the bond and the running memory
    outside it -- is what lets the cache survive an ambient fold-in instead of
    being thrown away by one.

    Caching needs a prefix of roughly 1024 tokens before it engages at all, and
    it engages silently: under that, this costs nothing and saves nothing.
    Watch cache_read on the recorded usage to see which is happening.
    """
    blocks = [{
        "type": "text",
        "text": system.stable,
        "cache_control": {"type": "ephemeral"},
    }]
    if system.volatile:
        blocks.append({"type": "text", "text": system.volatile})
    return blocks


def _claude_usage(response, model: str) -> LLMUsage:
    u = getattr(response, "usage", None)
    if u is None:
        return LLMUsage(provider="claude", model=model)
    return LLMUsage(
        provider="claude",
        model=model,
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


def _ask_claude(system, question: str, history: list[tuple[str, str]],
                opts=_NO_OPTIONS) -> str:
    import anthropic  # pip install anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    model = _claude_model(opts)
    response = client.messages.create(
        model=model,
        max_tokens=800,
        system=_claude_system(system),
        messages=_claude_messages(question, history),
    )
    opts.report(_claude_usage(response, model))
    return "".join(b.text for b in response.content if b.type == "text").strip()


def _ask_claude_stream(system, question: str, history: list[tuple[str, str]],
                       opts=_NO_OPTIONS):
    import anthropic  # pip install anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    model = _claude_model(opts)
    with client.messages.stream(
        model=model,
        max_tokens=800,
        system=_claude_system(system),
        messages=_claude_messages(question, history),
    ) as stream:
        yield from stream.text_stream
        # Only reachable once the stream is exhausted, which is exactly when
        # the totals are final. get_final_message() is the SDK's own accessor;
        # accumulating deltas by hand would miss the cache counters entirely.
        opts.report(_claude_usage(stream.get_final_message(), model))


# ---------------------------------------------------------------------------
# Claude Code CLI in headless mode (`claude -p`) — answers with the Claude
# subscription login on this machine instead of a metered API key. Slower to
# first token than a direct API call (each question spawns a fresh process),
# but zero marginal cost. Needs `claude` on PATH and a logged-in session.
# ---------------------------------------------------------------------------


def _claude_cli_env() -> dict:
    """The server's environment minus ANTHROPIC_API_KEY. The CLI prefers an
    API key over the subscription login whenever one is set, so the key that
    LLM_PROVIDER_REPLY's `claude` provider needs would otherwise hijack the
    free path -- and fail it outright for a key the CLI cannot use."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _claude_cli_cmd(system, question: str, history: list[tuple[str, str]]) -> list[str]:
    """The CLI is one-shot, so the rolling history is folded into the prompt as
    a transcript; the persona + campaign notes replace the CLI's own system
    prompt so nothing of the coding-assistant default leaks into Whiskers."""
    convo = "".join(f"Player: {q}\nFamiliar: {a}\n\n" for q, a in history)
    model = os.environ.get("CLAUDE_CLI_MODEL", "sonnet")
    return [
        "claude", "-p", f"{convo}Player: {question}",
        "--system-prompt", system.text,
        "--model", model,
        "--max-turns", "1",  # a plain answer — never an agentic tool loop
        # Skip everything a coding session loads at startup that a spoken
        # answer never uses: built-in tools, MCP servers, settings files (and
        # their hooks/plugins), skills, and the on-disk session transcript.
        # Measured ~0.8s off every question. Not --bare: that ignores the
        # OAuth login this provider exists to use.
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]


def _ask_claude_cli(system, question: str, history: list[tuple[str, str]],
                    opts=_NO_OPTIONS) -> str:
    result = subprocess.run(
        _claude_cli_cmd(system, question, history),
        capture_output=True, text=True, timeout=120, env=_claude_cli_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exited {result.returncode}: {result.stderr.strip()[:500]}")
    answer = result.stdout.strip()
    if not answer:
        raise RuntimeError("claude CLI returned no output")
    # The CLI reports no token counts, so the call would otherwise be entirely
    # invisible to metering. Record that it happened; the tokens stay zero and
    # the usage panel says so rather than implying the answer was free.
    opts.report(LLMUsage(provider="claude-cli", model=os.environ.get("CLAUDE_CLI_MODEL", "sonnet")))
    return answer


def _ask_claude_cli_stream(system, question: str, history: list[tuple[str, str]],
                           opts=_NO_OPTIONS):
    """stream-json emits one JSON object per line; with --include-partial-messages
    the text arrives as content_block_delta events we can hand straight to the
    viseme/TTS pipeline. --verbose is required by the CLI for stream-json output."""
    cmd = _claude_cli_cmd(system, question, history) + [
        "--output-format", "stream-json", "--include-partial-messages", "--verbose",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_claude_cli_env(),
    )
    try:
        yielded = False
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") != "stream_event":
                continue
            delta = event.get("event", {}).get("delta", {})
            if delta.get("type") == "text_delta" and delta.get("text"):
                yielded = True
                yield delta["text"]
        proc.wait(timeout=15)
        if proc.returncode != 0 and not yielded:
            raise RuntimeError(
                f"claude CLI exited {proc.returncode}: {proc.stderr.read().strip()[:500]}"
            )
    finally:
        if proc.poll() is None:
            proc.kill()


# ---------------------------------------------------------------------------
# Local LLM on the home network — Ollama, LM Studio, llama.cpp, anything that
# speaks the OpenAI-compatible /v1/chat/completions API. The server and that PC
# share a LAN, so this is a plain outbound HTTP call; the iPad keeps talking to
# the familiar's URL exactly as before. Short connect timeout so a powered-off PC falls
# through to the next provider in the chain within a couple of seconds.
# ---------------------------------------------------------------------------


def _local_llm_config() -> tuple[str, str]:
    url = os.environ.get("LOCAL_LLM_URL")
    if not url:
        raise RuntimeError("LOCAL_LLM_URL is not set (e.g. http://192.168.1.50:11434)")
    model = os.environ.get("LOCAL_LLM_MODEL", "")
    return url.rstrip("/"), model


def _openai_messages(system, question: str, history: list[tuple[str, str]]) -> list:
    messages = [{"role": "system", "content": system.text}]
    for q, a in history:
        messages.append({"role": "user", "content": q})
        messages.append({"role": "assistant", "content": a})
    messages.append({"role": "user", "content": question})
    return messages


def _ask_local(system, question: str, history: list[tuple[str, str]],
               opts=_NO_OPTIONS) -> str:
    url, model = _local_llm_config()
    resp = _session.post(
        f"{url}/v1/chat/completions",
        json={
            "model": model,
            "messages": _openai_messages(system, question, history),
            "max_tokens": 800,
        },
        timeout=(2, 120),  # (connect, read): fail over fast if the PC is off
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _ask_local_stream(system, question: str, history: list[tuple[str, str]],
                      opts=_NO_OPTIONS):
    url, model = _local_llm_config()
    resp = _session.post(
        f"{url}/v1/chat/completions",
        json={
            "model": model,
            "messages": _openai_messages(system, question, history),
            "max_tokens": 800,
            "stream": True,
        },
        stream=True,
        timeout=(2, 120),
    )
    resp.raise_for_status()
    for line in resp.iter_lines():
        if not line.startswith(b"data: "):
            continue
        payload = line[6:]
        if payload == b"[DONE]":
            break
        data = json.loads(payload)
        delta = data.get("choices", [{}])[0].get("delta", {}).get("content")
        if delta:
            yield delta


_LLM_PROVIDERS = {
    "gemini": _ask_gemini,
    "claude": _ask_claude,
    "claude-cli": _ask_claude_cli,
    "local": _ask_local,
}

_LLM_STREAM_PROVIDERS = {
    "gemini": _ask_gemini_stream,
    "claude": _ask_claude_stream,
    "claude-cli": _ask_claude_cli_stream,
    "local": _ask_local_stream,
}


# ======================================================================
# Text-to-speech — one voice for the familiar on every device.
#
# A voice provider is a function  text -> (audio_bytes, mime_type).  Register
# it in _TTS_PROVIDERS and name it in TTS_PROVIDER to plug it in. The chain is
# tried left to right so you can put an expensive/best voice first and a free
# one behind it:
#
#     TTS_PROVIDER=elevenlabs,gemini
#
# synthesize_speech() returns None when nothing is configured or every provider
# fails — the browser then speaks with its own local voice instead.
# ======================================================================


class TTSUsage(NamedTuple):
    """What one synthesis consumed. Characters are the unit cloud providers
    bill in; milliseconds are what a self-hosted provider actually costs, since
    the expense there is a CPU core held for the duration."""
    provider: str
    chars: int = 0
    ms: int = 0


def synthesize_speech(text: str, voice: str = "", on_usage=None):
    """Speak `text` in `voice` with the first configured provider that succeeds.

    `voice` is the speaking familiar's voice reference -- a voices/ stem, a path,
    or "" to mean "whatever the environment defaults to". It is a per-call
    argument rather than process state on purpose: two familiars must be able to
    speak in two voices without one switching the other's, which is exactly what
    a module-level "current voice" global cannot do.

    Returns (audio_bytes, mime_type), or None to tell the caller to fall back
    to the browser's built-in speech synthesis.
    """
    chain = [p.strip().lower() for p in os.environ.get("TTS_PROVIDER", "").split(",") if p.strip()]
    for name in chain:
        provider = _TTS_PROVIDERS.get(name)
        if provider is None:
            logging.warning("Unknown TTS_PROVIDER %r — skipping", name)
            continue
        try:
            t0 = time.monotonic()
            result = provider(text, voice)
            elapsed = time.monotonic() - t0
            logging.info("TTS %s: %d chars in %.2fs (voice=%r)",
                         name, len(text), elapsed, voice or "default")
            if on_usage is not None:
                try:
                    on_usage(TTSUsage(provider=name, chars=len(text),
                                      ms=int(elapsed * 1000)))
                except Exception:
                    logging.exception("TTS usage callback failed; continuing")
            return result
        except Exception:
            logging.exception("TTS provider %r failed; trying next", name)
    return None


def _tts_gemini(text: str, voice: str = ""):
    """Google Gemini TTS (free tier, same key as the brain).

    Returns 16-bit PCM which we wrap in a WAV container so the browser can play
    it straight from an <audio> element.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    model = os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
    voice = os.environ.get("GEMINI_TTS_VOICE", "Charon")

    resp = _session.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key},
        json={
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
                },
            },
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        part = data["candidates"][0]["content"]["parts"][0]["inlineData"]
    except (KeyError, IndexError):
        raise RuntimeError(f"Unexpected Gemini TTS response: {data}")

    pcm = _b64decode(part["data"])
    rate = _rate_from_mime(part.get("mimeType", ""))
    return _pcm_to_wav(pcm, rate), "audio/wav"


def _tts_elevenlabs(text: str, voice: str = ""):
    """ElevenLabs — a designed character voice (free tier ~10 min/month).

    Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID; returns MP3.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID")
    if not api_key or not voice_id:
        raise RuntimeError("ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID must be set")
    model = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")

    resp = _session.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": api_key, "accept": "audio/mpeg"},
        json={"text": text, "model_id": model},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content, "audio/mpeg"


def _tts_piper(text: str, voice: str = ""):
    """Piper — free neural TTS running in-process on this machine.

    No quota, no network, ~10-20x faster than realtime on a desktop CPU. PIPER_VOICE is
    either an official catalogue name (resolved to voices/<name>.onnx; download
    with `python -m piper.download_voices --data-dir voices <name>`) or an
    absolute path to any community .onnx voice.
    """
    loaded = _piper_load(voice)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        with _piper_lock:  # one synthesis at a time; the model isn't thread-safe
            loaded.synthesize_wav(text, wav_file)
    return buf.getvalue(), "audio/wav"


# Loaded Piper voices, keyed by resolved .onnx path. Several familiars can be
# speaking different voices, so this is a small cache rather than one slot --
# but it is still bounded, because each load is ~300MB.
_piper_voices: dict[str, object] = {}
_piper_lock = threading.Lock()
_PIPER_CACHE_MAX = 3


def _piper_path(voice: str) -> Path:
    """Resolve a voice reference to a .onnx file. A bare name comes from the
    catalogue in voices/; anything ending .onnx is taken as a path.

    A familiar's picked `voice` is usually a pocket-tts stem (the picker only
    validates .safetensors/.wav files), so a stem with no .onnx behind it falls
    back to the configured PIPER_VOICE rather than failing every synthesis.
    Only a missing *default* is an error worth stopping for.
    """
    default = os.environ.get("PIPER_VOICE", "en_GB-alan-medium")
    for name in ([voice, default] if voice else [default]):
        path = Path(name)
        if path.suffix != ".onnx":
            path = VOICES_DIR / f"{name}.onnx"
        elif not path.is_absolute():
            path = VOICES_DIR.parent / path
        if path.exists():
            return path
    raise RuntimeError(
        f"Piper voice not found: {path} — download it with "
        f"`python -m piper.download_voices --data-dir voices {default}`"
    )


def _piper_load(voice: str = ""):
    """Load a Piper voice and keep it in memory (a second or so, ~300MB each)."""
    path = _piper_path(voice)
    key = str(path)
    with _piper_lock:
        loaded = _piper_voices.get(key)
        if loaded is None:
            from piper import PiperVoice  # pip install piper-tts

            loaded = PiperVoice.load(path)
            if len(_piper_voices) >= _PIPER_CACHE_MAX:
                _piper_voices.pop(next(iter(_piper_voices)))  # oldest out
            _piper_voices[key] = loaded
        return loaded


def _tts_pocket(text: str, voice: str = ""):
    """pocket-tts — Kyutai's CPU-only neural TTS, running in-process like Piper.

    `voice` is a voices/ stem (e.g. "femwhiskers"), a preset name (alba, anna,
    giovanni, ...) or a path to a .safetensors voice state / .wav clip to clone
    from; "" falls back to POCKET_VOICE. The model is loaded once and shared;
    voice states are cached per voice, so two familiars can speak in two voices
    without either switching the other's.
    """
    try:
        model, voice_state = _pocket_load(voice)
    except Exception:
        # A persisted stem whose file has vanished (voices/ is gitignored and
        # outside table backup) reaches _pocket_load looking like a preset name
        # and raises. The familiar keeps a voice — the default — rather than
        # dropping to the browser's robot on every call.
        if not voice:
            raise
        logging.exception("pocket voice %r unusable; falling back to the default voice", voice)
        model, voice_state = _pocket_load("")
    with _pocket_lock:  # the model isn't thread-safe; one synthesis at a time
        audio = model.generate_audio(voice_state, text)  # 1-D float torch tensor
    # The model has no speed control, so POCKET_SPEED slows/speeds playback by
    # under-/over-stating the sample rate: <1 is slower and deeper, >1 faster and
    # higher (pitch shifts with it — it's not a pitch-preserving stretch).
    speed = _pocket_speed()
    rate = int(round(model.sample_rate * speed))
    return _pcm_to_wav(_float_to_pcm16(audio.numpy()), rate), "audio/wav"


def _pocket_speed() -> float:
    try:
        return max(0.5, min(2.0, float(os.environ.get("POCKET_SPEED", "1.0"))))
    except ValueError:
        return 1.0


# The shared model, plus voice states keyed by resolved voice reference. The
# model is the expensive part (~30MB download, loaded once); a voice state is
# cheap, so a small cache covers a table's familiars comfortably.
_pocket_model = None
_pocket_states: dict[str, object] = {}
_pocket_lock = threading.Lock()
_POCKET_CACHE_MAX = 8

# Clonable voices and catalogue voices both live here. These are shipped/shared
# assets, not campaign data: a familiar references one by name from its
# familiar.json rather than owning a copy.
VOICES_DIR = Path(__file__).resolve().parent / "voices"


def _resolve_pocket_voice(voice: str = "") -> tuple[str, str]:
    """Return (argument_for_get_state_for_audio_prompt, display_name).

    A bare stem is looked up in voices/; a path or preset name is passed through.
    An empty `voice` falls back to POCKET_VOICE from the environment.
    """
    wanted = (voice or os.environ.get("POCKET_VOICE", "alba")).strip()

    if not wanted.endswith((".safetensors", ".wav")) and "/" not in wanted:
        match = next(
            (p for p in VOICES_DIR.glob(f"{wanted}.*") if p.suffix in (".safetensors", ".wav")),
            None,
        )
        if match is not None:
            return str(match), wanted
        return wanted, wanted  # a pocket-tts preset name (alba, anna, ...)

    path = Path(wanted)
    if not path.is_absolute():
        path = VOICES_DIR.parent / wanted
    if not path.exists():
        raise RuntimeError(f"pocket-tts voice not found: {path}")
    return str(path), path.stem


def _pocket_load(voice: str = ""):
    """The shared model plus the voice state for `voice`, both cached.

    Slow the first time (model download, then the voice-state build); a second
    voice afterwards pays only for its own state.
    """
    global _pocket_model
    arg, name = _resolve_pocket_voice(voice)
    with _pocket_lock:
        if _pocket_model is None:
            from pocket_tts import TTSModel  # pip install pocket-tts (pulls torch)

            _pocket_model = TTSModel.load_model()
        state = _pocket_states.get(arg)
        if state is None:
            state = _pocket_model.get_state_for_audio_prompt(arg)
            if len(_pocket_states) >= _POCKET_CACHE_MAX:
                _pocket_states.pop(next(iter(_pocket_states)))
            _pocket_states[arg] = state
        return _pocket_model, state


def list_pocket_voices() -> list[str]:
    """Names of clonable voices sitting in voices/ (.safetensors or .wav files),
    for the browser control that picks a familiar's voice."""
    return sorted({p.stem for p in VOICES_DIR.glob("*") if p.suffix in (".safetensors", ".wav")})


def resolve_voice_name(voice: str) -> str:
    """The display name for a voice reference, without loading anything."""
    try:
        return _resolve_pocket_voice(voice)[1]
    except RuntimeError:
        return ""


def voice_exists(name: str) -> bool:
    """Whether `name` is a usable voice stem in voices/."""
    return bool(name) and any(
        p.suffix in (".safetensors", ".wav") for p in VOICES_DIR.glob(f"{name}.*")
    )


def _float_to_pcm16(samples) -> bytes:
    """pocket-tts yields float PCM in [-1, 1]; browsers want 16-bit PCM in a WAV."""
    import numpy as np

    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


_TTS_PROVIDERS = {
    "pocket": _tts_pocket,
    "piper": _tts_piper,
    "gemini": _tts_gemini,
    "elevenlabs": _tts_elevenlabs,
}


def _b64decode(data: str) -> bytes:
    import base64

    return base64.b64decode(data)


def _rate_from_mime(mime: str, default: int = 24000) -> int:
    # e.g. "audio/L16;codec=pcm;rate=24000"
    for field in mime.split(";"):
        field = field.strip()
        if field.startswith("rate="):
            try:
                return int(field.split("=", 1)[1])
            except ValueError:
                pass
    return default


def _pcm_to_wav(pcm: bytes, rate: int, channels: int = 1, sample_width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sample_width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()
