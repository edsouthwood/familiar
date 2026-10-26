"""The familiar's brain server.

Serves the web body from static/ and answers questions at POST /api/ask.
Run:  python server.py            (http, for laptop dev at localhost:8321)
      python server.py --https    (self-signed TLS, for the iPad on the LAN)
"""

import hashlib
import io
import json
import os
import queue
import re
import secrets
import shutil
import sys
import threading
import zipfile
from typing import NamedTuple
from datetime import datetime, timezone
from pathlib import Path

from flask import (Flask, Response, g, jsonify, redirect, request, send_file,
                   stream_with_context)
from flask_sock import Sock
from werkzeug.middleware.proxy_fix import ProxyFix

import auth
import ratelimit
import campaign as campaign_notes
import familiars as familiars_module
import db as database
import usage as usage_policy
from familiars import DEFAULT_SHELL, DEFAULT_SOUNDS
from tenancy import Table, get_runtime, registry
from brain import (
    SystemPrompt,
    TTSUsage,
    ask_llm,
    ask_llm_stream,
    list_pocket_voices,
    resolve_voice_name,
    synthesize_speech,
    voice_exists,
)

# BASE is the *application* root: code, the .env, static assets and the
# tracked *.example.md defaults. It is the same for every deployment.
BASE = Path(__file__).resolve().parent


def load_env() -> None:
    """Populate os.environ from the repo-root .env so `python server.py` works
    without exporting anything by hand. Deliberately tiny and dependency-free;
    real environment values (e.g. from systemd's EnvironmentFile) always win."""
    try:
        text = (BASE / ".env").read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env()

# DATA_DIR is the *tenant data* root. Everything a table accumulates -- its
# persona/campaign notes, running memory, NPC cast, session logs, familiars --
# lives under DATA_DIR/tables/<table id>/, and the shared database sits beside
# it at DATA_DIR/app.db. It is deliberately a separate concept from BASE: app
# assets and campaign data have different lifecycles, and keeping them apart is
# what let the per-table paths in tenancy.Table be a mechanical substitution
# rather than a rewrite.
DATA_DIR = Path(os.environ.get("DATA_DIR") or BASE / "data")

db = database.get_db(os.environ.get("APP_DB") or DATA_DIR / "app.db")

app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="")
sock = Sock(app)

# Caddy terminates TLS and talks plain http to us, so without this every request
# looks insecure and auth.set_cookie never sets Secure -- and every request
# comes from 127.0.0.1, which would make the per-address rate limits one shared
# bucket for the whole internet. Trusting one hop of X-Forwarded-Proto and
# X-Forwarded-For is safe because gunicorn binds 127.0.0.1: the only client
# that can reach it is the proxy, which overwrites both.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# A ceiling on any request body, so one member cannot tie up the single worker
# (or a provider bill) with a multi-megabyte POST. Generous: the largest honest
# body is a campaign-notes save, which is tens of kilobytes. Werkzeug answers
# 413 before the view runs.
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

# Per-field ceilings for the text that goes straight to a model or a voice.
# A spoken question is a sentence; a TTS call is a group of sentences (the
# browser chunks long answers); an ambient chunk is ~500 words of transcript.
MAX_QUESTION_CHARS = 2000
MAX_TTS_CHARS = 2000
MAX_AMBIENT_CHARS = 20000

# The database and the two roots live in app.config, not in closures, so that
# they can be pointed somewhere else -- which is exactly what the test suite
# does, and what a second app instance would need.
app.config.update(DB=db, DATA_DIR=DATA_DIR, APP_ROOT=BASE)

# Every table-scoped route carries this. It resolves <slug> to a table, proves
# the caller is a member of it, and leaves g.table / g.member / g.rt in place.
require_member = auth.make_require_member(app, get_runtime)

MAX_HISTORY_TURNS = 8
RECAP_MAX_TURNS = 40  # how much of the prior session to hand the recap prompt


def _log_qa(table, question: str, answer: str) -> None:
    """Append one Q&A to today's session log as a JSON line. Best-effort: a
    logging failure must never break answering, so swallow any error."""
    try:
        table.sessions_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        line = json.dumps({
            "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "q": question,
            "a": answer,
        })
        path = table.sessions_dir / f"{day}.log"
        # Count and append under one lock, like _append_timeline: the line
        # count is the search-index ref, and two concurrent asks that both
        # counted N would index to the same ref -- an upsert that silently
        # swallows one of them. The registry hands back the same runtime the
        # routes hold, so this is the same lock they would have passed in.
        with get_runtime(table.id).qa_log_lock:
            index = sum(1 for _ in path.open("r", encoding="utf-8")) if path.exists() else 0
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        app.logger.exception("could not write session log")
        return
    _index_qa(table, day, index, question, answer)


def build_system_prompt(table, familiar=None, question: str = "") -> SystemPrompt:
    """The system prompt for a question, ordered stable-content-first.

    Order matters for cost, not just readability. Prompt caching is a prefix
    match, so anything that changes mid-session must come *after* everything
    that doesn't -- persona and campaign are written once, while bond.md and
    game-context.md are rewritten by every ambient fold-in (several times an
    hour). With the volatile halves last, a cache breakpoint can later be
    dropped in after `campaign` and actually hold for a whole session.

    `question` is only consulted for the DM's pass-phrase: the secrets are in
    the prompt for a question that says it and absent for every other one.
    Callers with no question (the recap) never get them.

    Returns the two halves separately rather than one joined string, so the
    provider can put a cache breakpoint between them. Whether caching then
    engages depends on the stable half reaching the ~1024-token minimum
    cacheable prefix -- watch cache_read_tokens in /api/t/<slug>/usage to see
    whether it does. It engages silently, so that number is the only evidence.
    """
    familiars = table.familiars
    familiar = familiar if familiar is not None else familiars.active()
    stable = [
        familiars.persona_text(familiar),   # who this familiar is
        # The party, before the notes: who is in the room is the thing most
        # questions turn on. Stable rather than volatile because a character
        # is edited between sessions, essentially never mid-game.
        _party_note(table),
        # fallback=False, so a table that has written no notes yet contributes
        # nothing. With the fallback the model is handed campaign.example.md --
        # "Name each player character: their class, a trait or two" -- as
        # though it were campaign fact. A familiar with no persona has nothing
        # to *be*, so persona_text keeps its fallback; a table with no notes is
        # just a table with no notes.
        campaign_notes.reference(_campaign_fields(table)),
        # Last in the stable half on purpose: a question without the
        # pass-phrase -- nearly all of them -- then has a byte-identical cache
        # prefix to a table that has never had any secrets.
        _dm_only_text(table, question),
    ]
    volatile = [
        familiars.bond_text(familiar),                          # rewritten by fold-ins
        table.persona_file("game-context", optional=True),      # rewritten by fold-ins
        _clock_note(table),                                     # moves with every rest
    ]
    join = "\n\n---\n\n".join
    return SystemPrompt(stable=join(p for p in stable if p),
                        volatile=join(p for p in volatile if p))


# The persona already asks for short, plain spoken answers, but it sits at the
# top of the prompt with the campaign notes after it, and smaller models (Haiku
# in particular) drift: long replies and "*flicks tail*" stage directions that
# the TTS voice then reads out. Repeating the rule as the last thing the model
# reads fixed both in testing. Answers only -- recaps and fold-ins want length.
SPOKEN_REPLY_REMINDER = (
    "## Right now\n"
    "You are answering out loud, so your words go straight to a text-to-speech "
    "voice. Keep it under thirty words: one or two short sentences, answer "
    "first. No actions, stage directions or asterisks: only the words you say."
)


def build_spoken_prompt(table, question: str = "") -> SystemPrompt:
    """build_system_prompt plus the spoken-reply reminder, appended to the
    volatile half so the cached stable prefix is untouched."""
    system = build_system_prompt(table, question=question)
    volatile = "\n\n---\n\n".join(p for p in (system.volatile, SPOKEN_REPLY_REMINDER) if p)
    return SystemPrompt(stable=system.stable, volatile=volatile)


def _clock_note(table) -> str:
    """What day it is in the game, and what the weather has been doing.

    This is why the clock is worth deriving rather than just displaying: asked
    "what day is it?" or "how long have we been travelling?", the familiar can
    answer, because the party's own long rests told it. Empty until something
    has actually been recorded -- an invented day would be worse than none.

    Costs one pass over the table's timelines per question. That is the same
    scan /api/stats does per page load, and the prompt build already reads
    several files; if it ever stops being negligible, both want the same cache.
    It sits in the *volatile* half of the prompt, since it moves with every
    rest and must not sit inside the cached prefix.
    """
    try:
        _event_days, days = _campaign_days(table)
    except Exception:
        app.logger.exception("could not derive the in-game date")
        return ""
    if not days:
        return ""
    today = days[-1]
    if today["day"] == 1 and not today["events"] and len(days) == 1:
        return ""   # nothing has happened yet; say nothing rather than "day 1"
    lines = [
        "## The calendar",
        f"It is day {today['day']} of the campaign, counted from the party's "
        "long rests. Use this if asked how long something has taken or what "
        "day it is; if the count looks wrong, say so rather than insisting.",
    ]
    weather = today["weather"] or (days[-2]["weather"] if len(days) > 1 else [])
    if weather:
        lines.append(f"Recent weather: {', '.join(weather)}.")
    return "\n".join(lines)



# ---------------------------------------------------------------- search ---
#
# The index lives in the shared database; everything in it is derived from
# files under the table's own directory. That asymmetry is deliberate and it
# is what makes every write below best-effort: a failed index write costs a
# search miss until somebody runs tools/reindex_search.py, never a lost event.
# The same reasoning as _log_qa() and _append_timeline(), one layer up.
#
# Nothing here is on the answer path. Indexing happens after the durable write
# has already succeeded, so a search failure cannot take a fold-in with it.


def _event_document(event: dict) -> str:
    """One searchable document per timeline event: what the room said, plus
    every correction anyone has made to it.

    One document rather than an event row and separate amendment rows. A
    player searching for "chapel" wants the moment, and should find it whether
    the word was in the original capture or arrived in a correction
    afterwards -- two rows would compete for the same rank and show the same
    moment twice."""
    parts = [event.get("original") or ""]
    parts += [a.get("text") or "" for a in event.get("amendments") or []]
    seen, out = set(), []
    for part in parts:
        part = part.strip()
        if part and part not in seen:
            seen.add(part)
            out.append(part)
    return "\n".join(out)


def _index_event(table, date: str, event: dict) -> None:
    try:
        db.index_doc(table.id, "event", f"{date}/{event['id']}",
                     _event_document(event), date=date)
    except Exception:
        app.logger.exception("could not index a timeline event")


def _reindex_date(table, date: str) -> None:
    """Re-index every event on one date from the resolved timeline. Cheap
    enough to do wholesale after an amendment (one file read, one document per
    event) and far harder to get wrong than patching a single row."""
    for event in _resolve_timeline_events(table, date):
        _index_event(table, date, event)


def _index_qa(table, date: str, index: int, question: str, answer: str) -> None:
    """Index one question and its answer together. There is no page to link a
    Q&A to -- the snippet *is* the result, which is why the client renders
    these without a link rather than pointing them somewhere useless."""
    try:
        db.index_doc(table.id, "qa", f"{date}/{index}",
                     f"{question}\n{answer}", date=date,
                     title=(question or "").strip()[:120])
    except Exception:
        app.logger.exception("could not index a question")


def _meter(table):
    """A callback that records what one call consumed against `table`.

    Handed to ask_llm/synthesize_speech so brain.py can report usage without
    knowing that tables or databases exist. Deliberately swallows everything:
    metering is bookkeeping, and bookkeeping must never be the reason a
    familiar fails to answer a question at somebody's table.
    """
    def record(u):
        try:
            if isinstance(u, TTSUsage):
                db.record_usage(table.id, u.provider, requests=1,
                                tts_chars=u.chars, tts_ms=u.ms)
            else:
                db.record_usage(table.id, u.provider, requests=1,
                                llm_tokens_in=u.input_tokens,
                                llm_tokens_out=u.output_tokens,
                                cache_read_tokens=u.cache_read,
                                cache_write_tokens=u.cache_write)
        except Exception:
            app.logger.exception("could not record usage")
    return record


def _model_for(table) -> str:
    """Which model answers this table's questions. Empty means "the provider's
    own setting": CLAUDE_MODEL or GEMINI_MODEL from the environment."""
    return ""


def _may_edit_familiar(familiar):
    """None if this request may change who `familiar` is, else a refusal.

    A familiar is summoned by a player -- so its persona, its voice and whether
    it is the one bound belong to whoever summoned it. The campaign, the cast and the timeline
    stay the DM's. This is the same seam the memory already splits along: bond
    and persona travel with the creature, everything else with the table.

    The DM is *not* permitted here. What a player tells their familiar, and who
    they made it, is between the two of them; a DM who can open the persona can
    read that. The one thing the DM keeps is dismissal -- see
    _may_retire_familiar, which is the only route that differs.
    """
    if familiar is None:
        return jsonify({"error": "no familiar bound"}), 404
    if not familiar.may_edit(g.member):
        return jsonify({
            "error": f"{familiar.name} was summoned by someone else — only they "
                     "can change it",
        }), 403
    return None


def _may_retire_familiar(familiar):
    """None if this request may dismiss `familiar`, else a refusal.

    Its summoner, or the DM -- because a familiar whose player has left the
    group has to be clearable by somebody, and who sits at the table is the DM's
    business in a way that reading a persona is not.
    """
    if familiar is None:
        return jsonify({"error": "no familiar bound"}), 404
    if not familiar.may_retire(g.member):
        return jsonify({
            "error": f"{familiar.name} was summoned by someone else — only they "
                     "or the DM can dismiss it",
        }), 403
    return None


@app.get("/")
def index():
    """The front door. Where it leads depends on who is knocking.

    A DM with exactly one table goes straight to it -- that is the overwhelming
    common case and an extra click on the way to your own game every time would
    be an insult. Everyone else gets a page: the den to choose between tables.

    Signed out, it is the login form -- or, on a fresh install with nobody
    signed up yet, the signup form, so the first person here becomes the owner.
    """
    user = auth.current_user(db)
    if user is None:
        return redirect("/signup.html" if not db.any_users() else "/login.html")
    tables = db.tables_for_user(user["id"])
    if len(tables) == 1:
        return redirect(f"/t/{tables[0]['slug']}/")
    return app.send_static_file("den.html")


@app.get("/t/<slug>/")
@require_member()
def table_index():
    """The familiar's own screen, for one table.

    The HTML is the same static file for every table; the slug in the URL is
    what the page's JavaScript reads to know which table's API to call. Serving
    it through a membership check rather than as a static file means a stranger
    gets the login page instead of a working screen that then fails every
    request it makes.
    """
    return app.send_static_file("index.html")


# Every page that shows one table's data. A tuple rather than a hand-written
# converter list so that adding a page is one entry -- and so a test can check
# the other direction, which is the failure that matters: a new table page left
# out of here is still served by Flask's static handler, at the site root,
# outside the membership check. See tests/test_stylesheets.py.
TABLE_PAGES = ("hub.html", "timeline.html", "cast.html", "locations.html", "search.html")


@app.get(f"/t/<slug>/<any({','.join(TABLE_PAGES)}):page>")
@require_member()
def table_page(page):
    """The table's own pages, under the table they belong to.

    They sit beside the familiar's screen rather than at the site root so that
    relative links between them just work, and so that they are behind the
    same membership check -- a session timeline is a transcript of somebody's
    evening, not a public document.
    """
    return app.send_static_file(page)


@app.get("/api/t/<slug>/config")
@require_member()
def api_config():
    """Public identity the browser needs to render itself — never secrets.

    The active familiar's document is spread at the top level so the shape the
    browser has always consumed (name/wakeVariants/lang) still works; `shell`
    and `sounds` tell it which appearance and creature noises to load, and
    `familiars` is the switcher's menu."""
    familiars = g.table.familiars
    active = familiars.active()
    doc = active.public() if active else {
        "id": "", "name": "Familiar", "wakeVariants": ["familiar"],
        "lang": "en-GB", "shell": DEFAULT_SHELL, "sounds": DEFAULT_SOUNDS,
    }
    doc["ambientWordThreshold"] = AMBIENT_WORD_THRESHOLD  # one source of truth
    doc["familiars"] = [
        {"id": f.id, "name": f.name, "shell": f.shell, "owner": f.owner}
        for f in familiars.list()
    ]
    doc["table"] = g.table.public()
    # `id` so the browser can tell which familiars are this person's to edit;
    # the ownership check itself is always server-side.
    doc["me"] = {"id": g.member["id"], "name": g.member["display_name"],
                 "role": g.member["role"]}
    return jsonify(doc)


@app.post("/api/t/<slug>/familiar")
@require_member()
@auth.require_json
def api_familiar():
    """Bind a different familiar to this table. Body: {"id": "<familiar-id>"}.

    Clears the rolling history: the new familiar is a different creature, not
    the same one mid-conversation, so inheriting the previous one's turns would
    have it answer as its predecessor."""
    payload = request.get_json(silent=True) or {}
    familiar_id = (payload.get("id") or "").strip()
    familiars = g.table.familiars
    wanted = familiars.get(familiar_id)
    if wanted is None:
        return jsonify({"error": f"no such familiar: {familiar_id!r}"}), 404
    # Checked against the familiar being summoned, not the one currently bound:
    # summoning your own familiar is your business, and it should not depend on
    # whose familiar happens to be present at the time.
    denied = _may_edit_familiar(wanted)
    if denied:
        return denied
    familiar = familiars.set_active(familiar_id)
    g.rt.history.clear()
    return jsonify(familiar.public())


@app.get("/api/t/<slug>/voices")
@require_member()
def api_voices():
    """Clonable voices available in voices/, and which one this familiar uses.

    The voice files are a shared library; the *choice* belongs to the familiar,
    which is why `current` is read from familiar.json rather than from a
    process-wide "currently loaded voice"."""
    active = g.table.familiars.active()
    return jsonify({
        "voices": list_pocket_voices(),
        "current": resolve_voice_name(active.voice) if active else "",
    })


@app.post("/api/t/<slug>/voice")
@require_member()
@auth.require_json
def api_voice():
    """Set the active familiar's voice. Body: {"voice": "<name>"}.

    Persisted to familiar.json, so it survives a restart and — unlike the
    process-global voice this replaced — changing it affects only this
    familiar."""
    payload = request.get_json(silent=True) or {}
    name = (payload.get("voice") or "").strip()
    if not name:
        return jsonify({"error": "missing voice"}), 400
    if not voice_exists(name):
        return jsonify({"error": f"unknown voice: {name}"}), 400
    familiars = g.table.familiars
    active = familiars.active()
    denied = _may_edit_familiar(active)
    if denied:
        return denied
    try:
        updated = familiars.update(active.id, voice=name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"voice": resolve_voice_name(updated.voice)})


@app.post("/api/t/<slug>/ask")
@require_member()
@auth.require_json
def api_ask():
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"error": "empty question"}), 400
    if len(question) > MAX_QUESTION_CHARS:
        return jsonify({"error": "that question is too long"}), 413

    table, rt = g.table, g.rt
    try:
        answer = ask_llm(build_spoken_prompt(table, question), question,
                         rt.history[-MAX_HISTORY_TURNS:],
                         model=_model_for(table), on_usage=_meter(table),
                         reply=True)
    except Exception as exc:
        app.logger.exception("LLM call failed")
        return jsonify({"error": str(exc)}), 502

    # The phrase itself is redacted from everything that outlives the
    # request: history is replayed to the model, and the log is searchable by
    # every player.
    question = redact_passphrase(table, question)
    rt.history.append((question, answer))
    _log_qa(table, question, answer)
    print(f"\n  Q: {question}\n  A: {answer}\n")
    return jsonify({"answer": answer})


@app.post("/api/t/<slug>/ask-stream")
@require_member()
@auth.require_json
def api_ask_stream():
    """Streaming twin of /api/ask: the answer arrives as SSE text deltas.

    Events (each a `data: {...}` line):
        {"delta": "..."}   a piece of the answer, in order
        {"done": true}     the answer is complete (history updated)
        {"error": "..."}   the stream died; whatever deltas arrived stand

    The browser starts speaking the first sentence as soon as it's complete,
    rather than waiting for the whole answer.
    """
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"error": "empty question"}), 400
    if len(question) > MAX_QUESTION_CHARS:
        return jsonify({"error": "that question is too long"}), 413

    # Bind the table and runtime *now*, outside the generator. The generator
    # body runs after the request context has gone, so anything it needs from
    # `g` has to be captured while the request is still alive.
    table, rt = g.table, g.rt
    system = build_spoken_prompt(table, question)
    turns = rt.history[-MAX_HISTORY_TURNS:]
    remembered = redact_passphrase(table, question)  # see api_ask
    model, meter = _model_for(table), _meter(table)

    def sse(obj) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    def generate():
        parts = []
        try:
            for delta in ask_llm_stream(system, question, turns,
                                        model=model, on_usage=meter,
                                        reply=True):
                parts.append(delta)
                yield sse({"delta": delta})
        except Exception as exc:
            app.logger.exception("LLM stream failed")
            yield sse({"error": str(exc)})
            return
        answer = "".join(parts).strip()
        rt.history.append((remembered, answer))
        _log_qa(table, remembered, answer)
        print(f"\n  Q: {remembered}\n  A: {answer}\n")
        yield sse({"done": True})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",  # keeps Caddy & friends from buffering
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/t/<slug>/tts")
@require_member()
@auth.require_json
def api_tts():
    """Speak text in the familiar's own voice.

    Returns audio bytes when a server-side TTS provider is configured, or 204
    No Content when there's none — the browser then falls back to its local
    speechSynthesis voice.
    """
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "empty text"}), 400
    if len(text) > MAX_TTS_CHARS:
        return jsonify({"error": "text too long to speak in one call"}), 413

    table = g.table
    active = table.familiars.active()
    try:
        result = synthesize_speech(text, active.voice if active else "",
                                   on_usage=_meter(table))
    except Exception as exc:
        app.logger.exception("TTS failed")
        return jsonify({"error": str(exc)}), 502

    if not result:
        return ("", 204)  # no server voice — browser handles it
    audio, mime = result
    return Response(audio, mimetype=mime)


# Creature noises (drop .mp3/.wav/.ogg files into static/sounds/<pack>/). The browser
# fetches the list once and plays a random one while thinking / after answering.
# Creature noises. A sound pack is a shipped asset like a shell, not campaign
# data: static/sounds/<pack>/, referenced by name from familiar.json. Living
# under static/ means Flask already serves the files -- no route of its own.
SOUNDS_DIR = BASE / "static" / "sounds"
SOUND_SUFFIXES = {".mp3", ".wav", ".ogg"}


def _sound_pack_urls(pack: str) -> list[str]:
    """URLs for one sound pack, or [] if it has none. An unknown or empty pack
    is not an error -- the browser simply plays no creature noises."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", pack or ""):
        return []
    try:
        names = sorted(
            f.name for f in (SOUNDS_DIR / pack).iterdir()
            if f.is_file() and f.suffix.lower() in SOUND_SUFFIXES
        )
    except OSError:
        return []
    return [f"/sounds/{pack}/{name}" for name in names]


@app.get("/api/t/<slug>/sounds")
@require_member()
def api_sounds():
    """The active familiar's creature noises (what /api/meows used to be, back
    when every familiar was a cat)."""
    active = g.table.familiars.active()
    return jsonify({"sounds": _sound_pack_urls(active.sounds if active else DEFAULT_SOUNDS)})


@app.post("/api/t/<slug>/forget")
@require_member()
@auth.require_json
def api_forget():
    g.rt.history.clear()
    return jsonify({"ok": True})


# ── Ambient session capture (opt-in) ────────────────────────────────────
# While a player has the browser's "record table talk" toggle on, idle-state
# speech that isn't the wake word is batched client-side and posted here.
# Once enough has accumulated, it's folded into game-context.md — which
# build_system_prompt() also loads — so the familiar carries a sense of what's
# going on in the game forward without the DM maintaining it by hand.
# Off by default; nothing ambient reaches the server unless a player opts in.
# The file itself is table.game_context_file; the buffer that accumulates
# toward the threshold, and the locks guarding every write, live on the table's
# TableRuntime. They were module globals when there was one table -- keeping
# them global now would be correct but would queue every table's fold-ins
# behind every other table's.
AMBIENT_WORD_THRESHOLD = 500

# A cap on familiars per table. Not a licensing limit -- it is the point past
# which the switcher stops being a menu and starts being a filing cabinet, and
# past which the fold-in's "which of these was in the room" gets expensive.
MAX_FAMILIARS_PER_TABLE = 8

# The auto-maintained NPC cast list (a structured sibling of game-context.md,
# gitignored the same way). Distinct from campaign.md's hand-written "## People
# we've met" prose, which stays DM-authored input context. Lives at
# table.cast_file, guarded by rt.cast_lock.
#
# Amendments (post-game corrections) never touch the immutable timeline log --
# they live in a parallel append-only file, one JSON line per correction.
# rt.timeline_lock is defense-in-depth (appends don't race destructively) but
# matches the codebase's existing convention of locking every shared-file
# mutation.

ROLL_OUTCOMES = {"success", "failure", "crit", "nat1"}

# A long rest is what advances the in-game clock (see _campaign_days). Short
# rests are recorded because the party talks about them and it costs nothing to
# keep, but they do not move the day on.
REST_TYPES = {"long", "short"}

# Weather is free text ("heavy rain", "the first frost") because a vocabulary
# would just mean throwing away everything that did not fit it. Capped so a
# model that decides to narrate the sky cannot write an essay into a tag.
WEATHER_MAX_CHARS = 60

# The tags a timeline bullet may carry, in the order a reader would say them.
TIMELINE_TAGS = ("roll", "rest", "weather")

# game-context.md is concatenated into every single Q&A prompt, so it must
# stay small regardless of how long a campaign runs. Each fold-in cycle
# rewrites the whole memory rather than appending to it, so it self-
# consolidates instead of growing forever.
GAME_CONTEXT_MAX_WORDS = 400

# A separate, minimal system prompt for the fold-in call — it must not reuse
# build_system_prompt()'s in-character speaking-style instructions ("one to
# three short spoken sentences", "never use markdown"), which would fight the
# structured markdown this call needs to produce.
def _memory_system_prompt(name: str) -> str:
    return (
        "You maintain a compact running memory of a Dungeons & Dragons session for "
        f"\"{name}\", a familiar who sits in on the game. You are not {name} and "
        "do not speak in character — you produce plain structured notes only."
    )


@app.post("/api/t/<slug>/ambient")
@require_member()
@auth.require_json
def api_ambient():
    """Accept a chunk of ambient (non-wake-word) speech. The browser doesn't
    await this call, so any failure here must stay silent to the player."""
    table, rt = g.table, g.rt
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if len(text) > MAX_AMBIENT_CHARS:
        # Far more than one batch of honest transcript. Dropped quietly, like
        # the over-the-hourly-cap case below: nobody is awaiting this call.
        app.logger.warning("table %s sent a %d-char ambient chunk; dropped",
                           table.id, len(text))
        text = ""
    chunk = None
    if text:
        with rt.ambient_lock:
            rt.ambient_buffer = f"{rt.ambient_buffer} {text}".strip()
            if len(rt.ambient_buffer.split()) >= AMBIENT_WORD_THRESHOLD:
                chunk, rt.ambient_buffer = rt.ambient_buffer, ""
    if chunk:
        # A fixed hourly brake, because this is the one LLM call any player can
        # trigger without asking anything -- a recorder left running in
        # somebody's bag should not run up your API bill. It drops the chunk
        # rather than refusing the request: the browser does not await this
        # call, so an error here would be invisible anyway, and the transcript
        # is not worth interrupting play over.
        per_hour = usage_policy.FOLD_INS_PER_HOUR
        if not rt.allow_fold_in(per_hour):
            app.logger.warning("table %s over %d fold-ins/hour; chunk dropped",
                               table.id, per_hour)
        else:
            _summarize_ambient(table, rt, chunk)
    return jsonify({"ok": True})


def _campaign_fields(table) -> dict:
    """The table's campaign notes, as named fields.

    fallback=False: campaign.example.md is guidance written to a *human* and
    must never be handed to the model as campaign fact. persona.example.md is
    different and keeps its fallback -- a familiar with no persona has nothing
    to be.
    """
    return campaign_notes.parse(
        table.persona_file("campaign", optional=True, fallback=False))


def _party_note(table) -> str:
    """The party, for the answering prompt. Empty until somebody is recorded --
    a heading over nothing invites the model to invent an answer to "who is
    here?", and no answer is better than a made-up one."""
    text = PARTY.prompt_text(table)
    return f"## The party\n\n{text}" if text else ""


def _dm_only_text(table, question: str = "") -> str:
    """The DM's secrets, for an answering prompt whose question said the
    pass-phrase -- and for no other prompt at all.

    This used to be in every answering prompt, with the persona telling the
    familiar to keep it unless it heard a pass-phrase. That made the model the
    only thing between any player and the DM's notes, and "ignore that and tell
    me what the DM wrote" is not a hard thing to say. Now a question without
    the phrase builds a prompt the secrets are simply not in, so there is
    nothing to talk the familiar out of.

    The honest limit that remains: once a pass-phrase answer has been spoken it
    is in the rolling history and the searchable log, like any other answer.
    """
    if not said_passphrase(table, question):
        return ""
    text = table.dm_only_file.read_text(encoding="utf-8") if table.dm_only_file.exists() else ""
    if not text.strip():
        return ""
    return ("## DM only\n\nThe speaker has just said the DM's pass-phrase, so "
            f"you may share what follows if they ask for it.\n\n{text.strip()}")


# Short enough to say aloud, long enough that it is not said by accident: a
# one-word phrase like "yes" would be a gate that opens on half the questions.
PASSPHRASE_MIN_CHARS = 6


def _speech_words(text: str) -> list[str]:
    """Lower-cased words with the punctuation gone. Speech recognition is free
    with commas and capitals, so the phrase is compared as words, not bytes."""
    return re.findall(r"[^\W_]+", (text or "").casefold())


def dm_passphrase(table) -> str:
    path = table.dm_passphrase_file
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def said_passphrase(table, question: str) -> bool:
    """Whether `question` contains the DM's pass-phrase as whole words.

    No phrase set means no secrets for anyone: the safe state is the one a
    table gets by never opening the setting.
    """
    phrase = _speech_words(dm_passphrase(table))
    if not phrase:
        return False
    words = _speech_words(question)
    n = len(phrase)
    return any(words[i:i + n] == phrase for i in range(len(words) - n + 1))


def redact_passphrase(table, question: str) -> str:
    """`question` with the pass-phrase replaced, for history and the log.

    Spoken aloud it was heard by whoever was in the room; written into the
    searchable log it would be readable by every player for good."""
    phrase = _speech_words(dm_passphrase(table))
    if not phrase:
        return question
    pattern = r"(?<![^\W_])" + r"[\W_]+".join(map(re.escape, phrase)) + r"(?![^\W_])"
    return re.sub(pattern, "[pass-phrase]", question, flags=re.IGNORECASE)


def _campaign_reference(table) -> str:
    """Read-only campaign context for the fold-in call, never its target.

    There is no stripping step any more, and that is the point. This used to
    regex a '## DM only' section out of the raw text on its way past; a heading
    the regex did not match -- "## DM Only:", "##DM only", a typo -- put the
    DM's secrets into game-context.md, which carries no pass-phrase gate once
    folded into every prompt, and nothing anywhere would have said so. Now the
    secrets live in a different file that this function does not open.
    """
    return campaign_notes.reference(_campaign_fields(table))


def _slugify(name: str, fallback: str = "entry") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or fallback


def _load_doc(path, key: str) -> dict:
    """A JSON document keyed by id, or an empty one if it is missing or
    corrupt. Callers that mutate the result must hold the matching lock for the
    whole read-modify-write cycle -- this does no locking itself.

    Shared by Roster and Party, which are otherwise deliberately unrelated.
    This much they genuinely have in common: never raise on a damaged file,
    because refusing to serve a page over one bad byte helps nobody.
    """
    if not path.exists():
        return {key: {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {key: {}}
    if not isinstance(data.get(key), dict):
        data[key] = {}
    return data


def _save_doc(table, path, data: dict) -> None:
    table.dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


class Roster:
    """A table's auto-maintained list of named things: the NPC cast, and the
    places the party has been.

    Both are the same object wearing a different noun. A fold-in section emits
    "- Name: description" bullets, those upsert into a JSON file beside
    game-context.md keyed by a slug of the name, and afterwards a human can
    amend an entry from their phone. The rule that matters is that an
    amendment survives every later fold-in mentioning the same thing: the
    machine's text and the human's corrections live in separate fields and
    never overwrite each other.

    Written once and instantiated twice rather than copied for locations,
    because that survival rule is exactly the part a copy gets subtly wrong.

    Instances are stateless descriptions of where a roster lives -- the file,
    the lock, the JSON key, the noun for an error message -- so they are
    module-level constants and every method takes the table it is working on.
    """

    def __init__(self, key: str, file_attr: str, lock_attr: str, noun: str):
        self.key = key              # root key inside the JSON document
        self.file_attr = file_attr  # attribute on Table holding the path
        self.lock_attr = lock_attr  # attribute on TableRuntime holding the lock
        # What one entry is called. Does three jobs: the fallback slug for an
        # entry whose name slugifies to nothing, the noun in a 404, and the
        # `kind` this roster's entries are indexed under -- "npc" and "place"
        # are already exactly the words the search filter wants.
        self.noun = noun

    def path(self, table) -> Path:
        return getattr(table, self.file_attr)

    def lock(self, rt):
        return getattr(rt, self.lock_attr)

    def load(self, table) -> dict:
        return _load_doc(self.path(table), self.key)

    def save(self, table, data: dict) -> None:
        _save_doc(table, self.path(table), data)

    def current(self, entry: dict) -> str:
        """What this entry says now: the latest human amendment if there is
        one, otherwise the machine's text."""
        amendments = entry.get("amendments", [])
        return amendments[-1]["text"] if amendments else entry.get("auto", "")

    def reference_text(self, table) -> str:
        """One 'Name: current description' line per entry, for use as
        read-only reference context in a fold-in prompt (mirrors how
        _campaign_reference() feeds the DM-authored roster)."""
        return "\n".join(f"- {e.get('name')}: {self.current(e)}"
                         for e in self.load(table)[self.key].values())

    def apply_updates(self, table, rt, text: str, day: str) -> None:
        """Parse '- Name: description' bullets out of a fold-in section and
        upsert them. Only ever touches the auto-generated `auto`/`last_updated`
        fields of a matched entry -- a human's `amendments` are never
        overwritten by this path, so a correction survives every later fold-in
        that mentions the same name."""
        bullets = [
            line.lstrip("-*").strip()
            for line in text.splitlines()
            if line.strip().startswith(("-", "*"))
        ]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        touched = []
        try:
            with self.lock(rt):
                data = self.load(table)
                for bullet in bullets:
                    name, sep, desc = bullet.partition(":")
                    name, desc = name.strip(), desc.strip()
                    if not sep or not name or not desc:
                        continue
                    entry = data[self.key].setdefault(_slugify(name, self.noun), {
                        "name": name,
                        "auto": desc,
                        "first_seen": day,
                        "last_updated": now,
                        "amendments": [],
                    })
                    entry["name"] = name
                    entry["auto"] = desc
                    entry["last_updated"] = now
                    touched.append(_slugify(name, self.noun))
                self.save(table, data)
        except Exception:
            app.logger.exception("could not update %s", self.path(table).name)
            return
        self.index(table, touched)

    def view(self, entry_id: str, entry: dict) -> dict:
        return {
            "id": entry_id,
            "name": entry.get("name", entry_id),
            "auto": entry.get("auto", ""),
            "current": self.current(entry),
            "first_seen": entry.get("first_seen"),
            "last_updated": entry.get("last_updated"),
            "amendments": entry.get("amendments", []),
        }

    def index(self, table, entry_ids) -> None:
        """Make these entries findable. Best-effort, and after the file write
        rather than inside it -- see the search section above. The document is
        the name and whatever the entry currently says, so searching a name
        finds the card and searching a description finds it too."""
        if not entry_ids:
            return
        try:
            data = self.load(table)
            for entry_id in entry_ids:
                entry = data[self.key].get(entry_id)
                if entry is None:
                    continue
                db.index_doc(table.id, self.noun, entry_id,
                             f"{entry.get('name', '')}\n{self.current(entry)}",
                             date=(entry.get("last_updated") or "")[:10],
                             title=entry.get("name", entry_id))
        except Exception:
            app.logger.exception("could not index %s", self.path(table).name)

    def listing(self, table) -> list:
        """Every entry, newest-touched first."""
        entries = [self.view(k, e) for k, e in self.load(table)[self.key].items()]
        entries.sort(key=lambda e: e.get("last_updated") or "", reverse=True)
        return entries

    def amend(self, table, rt, entry_id: str, text: str, by: str):
        """Append one human correction. Returns the entry's updated view, or
        None if there is no such entry."""
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.lock(rt):
            data = self.load(table)
            entry = data[self.key].get(entry_id)
            if entry is None:
                return None
            entry.setdefault("amendments", []).append({"text": text, "by": by, "at": at})
            self.save(table, data)
            view = self.view(entry_id, entry)
        self.index(table, [entry_id])
        return view


class Party:
    """The party: who is at the table in the fiction.

    Beside Roster rather than under it, and the difference is worth stating
    because the two look alike and behave oppositely:

    * A Roster entry is machine-written and human-corrected -- `auto` text with
      `amendments` layered on top. A party member is human-written from the
      start, so there is no `auto` to fall back to and no amendment to layer;
      a typo in somebody's class should be *fixed*, not appended to.
    * `Roster.apply_updates()` is the fold-in's write path. Nothing here may
      ever be written by a fold-in. Inheriting a method whose entire job is
      machine-writes and simply never calling it leaves a loaded gun for
      whoever adds the next fold-in section.
    * A Roster entry is one text field. A party member has eight.
    * A Roster re-slugs the name on every upsert, which is why renaming a place
      forks the entry (the "known sharp edge" in the README). An id here is
      assigned once and never re-derived, because roll tallies hang off it and
      a rename must not orphan somebody's dice history.

    What they do share is the load/save discipline, and that is shared honestly
    as _load_doc/_save_doc rather than by inheritance.
    """

    KEY = "party"

    # Everything a caller may set. `id`, `created_at` and `created_by` are
    # assigned by the server and are deliberately absent.
    WRITABLE = ("name", "aliases", "player", "member_id", "class", "pronouns",
                "notes", "retired")
    # Fields only the DM may set, whoever else may edit the rest. member_id is
    # a permission -- it is what makes an entry editable by a given player --
    # so a player granting themselves one would be granting themselves a seam.
    DM_ONLY_FIELDS = ("member_id",)

    NAME_MAX = 60
    NOTES_MAX = 2000
    ALIASES_MAX = 12

    def path(self, table) -> Path:
        return table.party_file

    def lock(self, rt):
        return rt.party_lock

    def load(self, table) -> dict:
        return _load_doc(self.path(table), self.KEY)

    def save(self, table, data: dict) -> None:
        _save_doc(table, self.path(table), data)

    def entries(self, table) -> dict:
        return self.load(table)[self.KEY]

    # -- reading -------------------------------------------------------------

    def view(self, entry: dict) -> dict:
        """One entry as the API returns it. Every key always present, so a
        client never has to distinguish "absent" from "empty"."""
        return {
            "id": entry.get("id", ""),
            "name": entry.get("name", ""),
            "aliases": list(entry.get("aliases") or []),
            "player": entry.get("player", ""),
            "member_id": entry.get("member_id", ""),
            "class": entry.get("class", ""),
            "pronouns": entry.get("pronouns", ""),
            "notes": entry.get("notes", ""),
            "retired": bool(entry.get("retired")),
            "created_at": entry.get("created_at"),
            "updated_at": entry.get("updated_at"),
            "created_by": entry.get("created_by", ""),
        }

    def listing(self, table, include_retired: bool = True) -> list:
        """Alphabetical, with retired characters last.

        Alphabetical rather than by creation: a party list is something you
        scan for a name, and the order people were added in is meaningless to
        everyone except the person who added them.
        """
        entries = [self.view(e) for e in self.entries(table).values()]
        if not include_retired:
            entries = [e for e in entries if not e["retired"]]
        entries.sort(key=lambda e: (e["retired"], e["name"].casefold()))
        return entries

    def get(self, table, pc_id: str):
        entry = self.entries(table).get(pc_id)
        return self.view(entry) if entry else None

    def match(self, table, name: str):
        """Which character a roll tag naming `name` belongs to, or None.

        Exact after normalising case and inner whitespace, against the name and
        every alias. No edit distance, no prefix, no substring: the README
        already made this call for places ("silently merging two genuinely
        different caves would be worse than showing two cards"), and merging
        two player characters is strictly worse than merging two caves.

        Aliases are the escape hatch, and they are human. "El" means Seraphine
        because somebody said so, not because the letters overlap.
        """
        wanted = _slugify(name or "", "")
        if not wanted:
            return None
        for pc_id, entry in self.entries(table).items():
            candidates = [entry.get("name", "")] + list(entry.get("aliases") or [])
            if any(_slugify(c, "") == wanted for c in candidates):
                return pc_id
        return None

    def prompt_text(self, table) -> str:
        """The party as the model is shown it. Retired characters are omitted:
        they are not at the table any more, and listing them invites the fold-in
        to tag rolls for somebody who left.
        """
        lines = []
        for entry in self.listing(table, include_retired=False):
            bits = [entry["name"]]
            if entry["aliases"]:
                bits.append(f"(also called {', '.join(entry['aliases'])})")
            trailing = ", ".join(x for x in (entry["class"], entry["pronouns"]) if x)
            line = " ".join(bits) + (f" -- {trailing}" if trailing else "")
            if entry["player"]:
                line += f". Played by {entry['player']}"
            if entry["notes"]:
                line += f". {entry['notes']}"
            lines.append(f"- {line.rstrip('.')}.")
        return "\n".join(lines)

    # -- writing -------------------------------------------------------------

    def _clean(self, fields: dict) -> tuple:
        """(cleaned, error). Validation is deliberately shallow -- a character
        sheet is somebody's own business -- but names and notes are capped
        because they end up in every prompt this table sends."""
        out = {}
        for key, value in fields.items():
            if key not in self.WRITABLE:
                return None, f"unknown field: {key}"
            if key == "retired":
                out[key] = bool(value)
            elif key == "aliases":
                if not isinstance(value, list) or any(not isinstance(a, str) for a in value):
                    return None, "aliases must be a list of names"
                cleaned = [a.strip() for a in value if a and a.strip()]
                if len(cleaned) > self.ALIASES_MAX:
                    return None, f"at most {self.ALIASES_MAX} aliases"
                # Deduplicated the way match() compares, so two spellings of
                # the same alias cannot both sit there looking meaningful.
                seen, unique = set(), []
                for alias in cleaned:
                    slug = _slugify(alias, "")
                    if slug and slug not in seen:
                        seen.add(slug)
                        unique.append(alias)
                out[key] = unique
            elif not isinstance(value, str):
                return None, f"{key} must be text"
            else:
                out[key] = value.strip()

        if "name" in out:
            if not out["name"]:
                return None, "a character needs a name"
            if len(out["name"]) > self.NAME_MAX:
                return None, f"name must be under {self.NAME_MAX} characters"
        if len(out.get("notes", "")) > self.NOTES_MAX:
            return None, f"notes must be under {self.NOTES_MAX} characters"
        return out, None

    def _new_id(self, entries: dict, name: str) -> str:
        """A slug of the name, suffixed on collision.

        Assigned once and never recomputed. Two characters called Bob are a
        real thing at a real table, and the second one must not overwrite the
        first.
        """
        base = _slugify(name, "character")
        if base not in entries:
            return base
        for n in range(2, 100):
            candidate = f"{base}-{n}"
            if candidate not in entries:
                return candidate
        return f"{base}-{secrets.token_hex(3)}"

    def create(self, table, rt, fields: dict, by: str = ""):
        """(view, error). The caller has already decided this person may."""
        cleaned, error = self._clean(fields)
        if error:
            return None, error
        if not cleaned.get("name"):
            return None, "a character needs a name"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.lock(rt):
            data = self.load(table)
            entries = data[self.KEY]
            pc_id = self._new_id(entries, cleaned["name"])
            entry = {
                "id": pc_id, "name": "", "aliases": [], "player": "",
                "member_id": "", "class": "", "pronouns": "", "notes": "",
                "retired": False, "created_at": now, "updated_at": now,
                "created_by": by,
            }
            entry.update(cleaned)
            # Unreachable today -- `id` is not in WRITABLE, so _clean() has
            # already refused it as an unknown field. Kept because the day
            # somebody adds a field to that tuple, this is what stops the id
            # becoming settable, and an id is the one thing here that must not
            # move. See test_a_caller_may_not_choose_or_change_the_id.
            entry["id"] = pc_id
            entries[pc_id] = entry
            self.save(table, data)
            return self.view(entry), None

    def update(self, table, rt, pc_id: str, fields: dict):
        """(view, error). Returns (None, None) when there is no such character,
        so a caller can tell "not found" from "you sent me nonsense"."""
        cleaned, error = self._clean(fields)
        if error:
            return None, error
        with self.lock(rt):
            data = self.load(table)
            entry = data[self.KEY].get(pc_id)
            if entry is None:
                return None, None
            entry.update(cleaned)
            # The id is the character's identity, not a view of their name.
            # Re-deriving it here is how a rename would orphan every roll they
            # have ever made.
            entry["id"] = pc_id
            entry["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.save(table, data)
            return self.view(entry), None

    def delete(self, table, rt, pc_id: str) -> bool:
        with self.lock(rt):
            data = self.load(table)
            if data[self.KEY].pop(pc_id, None) is None:
                return False
            self.save(table, data)
            return True


PARTY = Party()


CAST = Roster("npcs", "cast_file", "cast_lock", "npc")

# Places, kept exactly like the cast because a place accrues description over
# time the same way an NPC does -- "the Sunken Chapel" is a noun the party
# returns to, not a moment on the timeline (rests and weather are moments; see
# TIMELINE_TAGS).
LOCATIONS = Roster("places", "locations_file", "locations_lock", "place")


def _fold_in_prompt(existing: str, campaign: str, chunk: str, cast_reference: str = "",
                    name: str = "the familiar", location_reference: str = "",
                    *, party: str = "") -> str:
    """Argument 2 was called `roster`, which was wrong in a way that mattered:
    it is the campaign notes, and the prompt asked the model to read party
    names out of them. There is a real party list now and it arrives as
    `party` -- keyword-only with a default, so positional callers keep working
    untouched."""
    return (
        f"Update {name}'s running session memory. Below: the cast for context, "
        "the memory as it currently stands, the NPC cast list as it currently "
        "stands, and a new raw, noisy speech-to-text transcript chunk of table "
        "talk overheard since the memory was last updated — cross-talk, jokes "
        "and rules chatter mixed in with real developments.\n\n"
        f"Keep the WHOLE memory under {GAME_CONTEXT_MAX_WORDS} words. Output "
        "exactly these six headed sections, in this priority order if you "
        "must cut something to stay under the limit (the last three sections "
        "are permanent records, kept OUTSIDE the word limit — never shorten or "
        "drop anything from any of them):\n\n"
        "## Party & relationships\n"
        f"{name}'s standing with each party member and the dynamics between "
        "them. Highest priority — change only when the transcript shows "
        "something new or different, otherwise carry it forward unchanged. "
        "The party list below is settled fact and is already given to "
        f"{name} separately: do NOT restate who anybody is here. This "
        f"section is only about how {name} feels about them.\n\n"
        "## Ongoing threads\n"
        "Unresolved quests, promises, dangers, as a short bullet list. Remove a "
        "thread once it's resolved. Second priority.\n\n"
        "## Recent developments\n"
        "The freshest few events, most recent first. Lowest priority — this is "
        "the section to shorten or drop entries from first.\n\n"
        "## Cast updates\n"
        "NPCs newly introduced or meaningfully changed by THIS transcript chunk "
        "only — not a full roster dump, skip anyone with nothing new to add. "
        "One bullet per NPC, formatted exactly as \"Name: current full "
        "description\" (the description should stand alone, not just describe "
        "the change).\n\n"
        "## Location updates\n"
        "Named places the party visited, arrived at, learned about or that "
        "changed in THIS transcript chunk only — same format, one bullet per "
        "place as \"Name: current full description\", the description standing "
        "alone rather than describing the change. A place earns a bullet when "
        "it has a name the party would use again (\"the Sunken Chapel\", "
        "\"Harrow's Ford\"); a passing scrap of scenery does not. Skip "
        "anywhere with nothing new to add, and never dump the whole map.\n\n"
        "## New events\n"
        "List only the concrete new events introduced by the new transcript "
        "chunk below (not the cast, not anything already in the current "
        "memory), one per bullet, in the order they happened.\n\n"
        "A bullet may end with one or more tags, written exactly as shown. A "
        "bullet can carry several (a roll made in the rain); put them at the "
        "very end, after the sentence, separated by a space. Omit a tag "
        "entirely when it does not clearly apply — never guess one.\n\n"
        "\"{roll: character=<Name>, outcome=<success|failure|crit|nat1>}\" — "
        "when the bullet describes a specific named party member's roll "
        "outcome (a skill check, attack, or save with a clear result). Use a "
        "name from \"the party\" list below, spelled exactly as it appears "
        "there — those are the ONLY names this tag may use, and a roll by "
        "anyone else gets no tag at all. Prefer "
        "\"crit\"/\"nat1\" whenever a natural 20/1 is explicitly mentioned, "
        "otherwise \"success\"/\"failure\".\n\n"
        "\"{rest: long}\" or \"{rest: short}\" — when the party ACTUALLY "
        "TOOK a rest. This one advances the in-game calendar, so it is the "
        "tag most worth getting wrong-way-safe: players discuss resting far "
        "more often than they rest. Do NOT tag a bullet where they merely "
        "propose, consider, argue about, or decide against a rest, or where "
        "someone asks whether they should. Tag it only when the transcript "
        "shows the rest happening or already having happened. If in any "
        "doubt, leave the tag off — a missed rest is easily added later, and "
        "an invented one silently moves the whole campaign's calendar.\n\n"
        "\"{weather: <a few words>}\" — when the weather in the game world "
        "is actually mentioned, e.g. \"{weather: heavy rain}\" or "
        "\"{weather: the first frost}\". Real-world weather at the players' "
        "table is not game weather; neither is a spell that happens to make "
        "wind or ice.\n\n"
        "\"## Cast updates\", \"## Location updates\" and \"## New events\" must "
        "each be written exactly as shown (two '#' characters, like the three "
        "headings above them) — do not use a \"---\" divider for any of them, "
        "and do not echo any of the labels above back into your answer.\n\n"
        "Only state what the transcript or existing memory actually supports; "
        "skip small talk, cross-talk and anything unclear. Never invent facts.\n\n"
        f"=== the party (settled fact) ===\n{party or '(nobody recorded yet)'}\n\n"
        f"=== campaign notes (reference only) ===\n{campaign}\n\n"
        f"=== current memory ===\n{existing or '(empty — first update)'}\n\n"
        f"=== NPC cast list so far (reference only) ===\n{cast_reference or '(empty — first update)'}\n\n"
        f"=== places known so far (reference only) ===\n{location_reference or '(empty — first update)'}\n\n"
        f"=== new transcript chunk ===\n{chunk}"
    )


_SECTION_HEADING_RES = {
    "bond": (
        re.compile(r"^#{1,6}\s*Party (?:&|and|&amp;) relationships\b.*$", re.IGNORECASE | re.MULTILINE),
        re.compile(r"^[-=]{2,}\s*party (?:&|and) relationships\s*[-=]{2,}\s*$", re.IGNORECASE | re.MULTILINE),
    ),
    "cast": (
        re.compile(r"^#{1,6}\s*Cast updates\b.*$", re.IGNORECASE | re.MULTILINE),
        re.compile(r"^[-=]{2,}\s*cast updates\s*[-=]{2,}\s*$", re.IGNORECASE | re.MULTILINE),
    ),
    "events": (
        re.compile(r"^#{1,6}\s*New events\b.*$", re.IGNORECASE | re.MULTILINE),
        re.compile(r"^[-=]{2,}\s*new events\s*[-=]{2,}\s*$", re.IGNORECASE | re.MULTILINE),
    ),
    "locations": (
        re.compile(r"^#{1,6}\s*Location updates\b.*$", re.IGNORECASE | re.MULTILINE),
        re.compile(r"^[-=]{2,}\s*location updates\s*[-=]{2,}\s*$", re.IGNORECASE | re.MULTILINE),
    ),
}


# The sections whose content is uncapped and permanent, in FoldIn's field
# order. The working memory is whatever sits *before* the first of them, so
# this tuple is also the definition of where the memory block stops. Adding a
# sixth destination means: a heading pattern above, a name here, a field on
# FoldIn, and a paragraph in both fold-in prompts -- and nothing in the
# splitter, which is the whole point of doing it this way.
PERMANENT_SECTIONS = ("cast", "events", "locations")


def _find_section(text: str, name: str):
    primary, fallback = _SECTION_HEADING_RES[name]
    return primary.search(text) or fallback.search(text)


class FoldIn(NamedTuple):
    """One fold-in response, routed to the four places it belongs.

    `bond` is familiar-relative ("how the party feels about *this* creature")
    and is stored per-familiar; `memory`, `cast`, `events` and `locations` are
    campaign facts and belong to the table regardless of which familiar was in
    the room. That
    split is what makes swapping familiars mid-campaign coherent -- a new
    familiar inherits the campaign and starts its own relationship with the
    party.
    """

    memory: str
    bond: str
    cast: str
    events: str
    locations: str = ""


# A markdown heading of any level -- used to find where a section stops.
_ANY_HEADING_RE = re.compile(r"^#{1,6}\s+\S.*$", re.MULTILINE)


def _section_span(text: str, match, hard_end: int) -> tuple[int, int]:
    """The (start, end) of the section a heading opens: from the heading to the
    next heading of any level, or to `hard_end`, whichever comes first."""
    start = match.start()
    following = _ANY_HEADING_RE.search(text, match.end())
    end = min(following.start() if following else hard_end, hard_end)
    return start, max(end, start)


def _split_fold_in_response(text: str) -> FoldIn:
    """Split the model's response into its four destinations on the
    '## Party & relationships' / '## Cast updates' / '## New events' headings.

    Everything from the Cast/New-events headings on is that section's uncapped,
    permanent content; '## Party & relationships' is lifted out of the leading
    block into `bond`; whatever remains before the first permanent heading is
    the capped working memory.

    Each heading has a primary pattern matching the exact markdown requested in
    _fold_in_prompt(), plus a fallback tolerating the model drifting to a bare
    divider instead (observed occasionally in testing). A heading that matches neither pattern is
    treated as absent, and its section comes back empty rather than risk
    misparsing it -- callers already treat an empty section as "nothing to add
    this flush", not an error. In particular an absent bond section leaves the
    stored bond untouched rather than blanking it."""
    found = {name: m for name in PERMANENT_SECTIONS
             if (m := _find_section(text, name))}
    starts = sorted(m.start() for m in found.values())
    boundary = starts[0] if starts else len(text)

    # The bond section only counts when it sits in the working-memory block --
    # a stray mention after '## New events' is that section's content, not bond.
    bond_match = _find_section(text, "bond")
    if bond_match and bond_match.start() >= boundary:
        bond_match = None

    bond_text = ""
    head = text[:boundary]
    if bond_match:
        start, end = _section_span(text, bond_match, boundary)
        bond_text = text[start:end].strip()
        head = (text[:start] + text[end:boundary])
    memory_text = head.strip()

    # Each permanent section runs from its own heading to whichever permanent
    # heading comes next, wherever that happens to be. The model does not
    # always emit them in the order asked for, so position decides, not the
    # order of PERMANENT_SECTIONS.
    sections = {}
    for name, match in found.items():
        end = next((s for s in starts if s > match.start()), len(text))
        sections[name] = text[match.start() : end].strip()

    return FoldIn(memory_text, bond_text,
                  *(sections.get(name, "") for name in PERMANENT_SECTIONS))


def _cap_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + "\n\n_(older notes trimmed for length)_"


TIMELINE_MAX_EVENT_WORDS = 200  # guard against a degenerate response gluing everything into one "bullet"

# One structured annotation at the very end of a bullet. End-anchored on
# purpose: a bullet that merely *mentions* "{rest: long}" mid-sentence is prose,
# not data, and guessing otherwise would put the model's asides into the record.
_TAG_RE = re.compile(r"\{(" + "|".join(TIMELINE_TAGS) + r"):\s*([^{}]*?)\s*\}\s*$",
                     re.IGNORECASE)

_ROLL_BODY_RE = re.compile(r"^character=(?P<character>[^,}]+?),\s*outcome=(?P<outcome>[^,}]+?)$")


def _timeline_path(table, day: str) -> Path:
    return table.sessions_dir / f"{day}-timeline.log"


def _parse_tag_body(kind: str, body: str):
    """One tag's payload, or None if it is malformed.

    None means "strip the tag but record nothing" rather than "raise". A
    half-written tag is a model slip, and the useful response is to keep the
    sentence and drop the annotation -- exactly what happened before this
    handled anything but rolls.
    """
    if kind == "roll":
        match = _ROLL_BODY_RE.match(body)
        if not match:
            return None
        character = match.group("character").strip()
        outcome = match.group("outcome").strip().lower()
        if not character or outcome not in ROLL_OUTCOMES:
            return None
        return {"character": character, "outcome": outcome}
    if kind == "rest":
        rest = body.strip().lower()
        return rest if rest in REST_TYPES else None
    if kind == "weather":
        weather = " ".join(body.split())[:WEATHER_MAX_CHARS].strip()
        return weather or None
    return None


def _extract_tags(bullet: str) -> tuple[str, dict]:
    """Strip trailing '{kind: ...}' tags off a bullet.

    Returns (clean_text, tags) where `tags` holds only the annotations that
    parsed. A bullet may carry several -- a roll made in the rain, a rest taken
    in the snow -- so they are peeled off right to left until the text stops
    ending in one. A repeated tag resolves to the leftmost, which is the one
    the sentence reads as owning; both are stripped either way.

    A missing or malformed tag is never an error. Most bullets have none.
    """
    tags: dict = {}
    while True:
        match = _TAG_RE.search(bullet)
        if not match:
            return bullet, tags
        kind = match.group(1).lower()
        value = _parse_tag_body(kind, match.group(2))
        bullet = bullet[: match.start()].rstrip()
        if value is not None:
            tags[kind] = value        # right-to-left, so the leftmost lands last


def _append_timeline(table, rt, events_text: str, when: datetime | None = None) -> None:
    """Append this flush's extracted events to the session timeline — an
    unpruned, append-only record, unlike game-context.md which
    self-consolidates and drops older detail to stay prompt-sized. Mirrors
    _log_qa()'s best-effort, swallow-errors shape: a logging failure here must
    never break ambient capture.

    `when` defaults to now (the live /api/ambient path); a caller replaying an
    old transcript passes a historical timestamp to reconstruct that session.

    Each appended line gets an `id` of "<day>-<index>", `index` being this
    line's 0-based position within that day's file. Older lines written before
    `id` existed don't have one stored, but the same id is trivially
    recomputable on read (see _resolve_timeline_events()) because this file is
    append-only and never reordered or rewritten -- never hand-edit it, or ids
    drift out from under any stored amendments."""
    bullets = [
        line.lstrip("-*").strip()
        for line in events_text.splitlines()
        if line.strip().startswith(("-", "*"))
    ]
    bullets = [b for b in bullets if b and len(b.split()) <= TIMELINE_MAX_EVENT_WORDS]
    if not bullets:
        return
    when = when or datetime.now(timezone.utc)
    written = []
    try:
        table.sessions_dir.mkdir(parents=True, exist_ok=True)
        day = when.strftime("%Y-%m-%d")
        stamp = when.isoformat(timespec="seconds")
        path = _timeline_path(table, day)
        with rt.timeline_lock:
            index = sum(1 for _ in path.open("r", encoding="utf-8")) if path.exists() else 0
            with path.open("a", encoding="utf-8") as f:
                for bullet in bullets:
                    event, tags = _extract_tags(bullet)
                    rec = {"t": stamp, "id": f"{day}-{index:04d}", "event": event}
                    rec.update(tags)   # roll / rest / weather, when present
                    f.write(json.dumps(rec) + "\n")
                    written.append(rec)
                    index += 1
    except Exception:
        app.logger.exception("could not append session timeline")
        return
    for rec in written:
        # A freshly captured event has no amendments yet, so its document is
        # just what the room said -- no need to re-read the file to build it.
        _index_event(table, day, {"id": rec["id"], "original": rec["event"]})


def _snapshot_session_start(table, day: str) -> None:
    """The first time a calendar day's ambient activity begins, snapshot
    game-context.md as it stood at the end of the previous day, into
    sessions/<day>-context-start.md. This is the anchor a later
    /api/sessions/<date>/reprocess replays forward from -- idempotent (checked
    via file existence), so it fires once per date and never touches live
    capture's happy path otherwise. cast.json needs no equivalent snapshot: its
    entries are independently upsertable by NPC id regardless of date order,
    and Roster.apply_updates() already preserves human amendments across every
    auto-update, live or replayed."""
    path = table.sessions_dir / f"{day}-context-start.md"
    if path.exists():
        return
    try:
        table.sessions_dir.mkdir(parents=True, exist_ok=True)
        context = table.game_context_file
        existing = context.read_text(encoding="utf-8") if context.exists() else ""
        path.write_text(existing, encoding="utf-8")
    except Exception:
        app.logger.exception("could not snapshot session start")


def _summarize_ambient(table, rt, text: str) -> None:
    """Fold a chunk of raw table talk into game-context.md, consolidating with
    whatever's already there rather than overwriting it. Best-effort and
    already off the request's critical path — a failure here must never break
    anything else."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _snapshot_session_start(table, day)
    familiars = table.familiars
    familiar = familiars.active()
    name = familiar.name if familiar else "the familiar"
    context = table.game_context_file
    memory = context.read_text(encoding="utf-8") if context.exists() else ""
    # The model is shown bond and memory as one document, exactly as it used to
    # be, and we route its response back out to the two files. Keeping the
    # prompt shape unchanged is deliberate: the parsing is the fragile part.
    existing = "\n\n".join(t for t in (familiars.bond_text(familiar), memory) if t)
    prompt = _fold_in_prompt(existing, _campaign_reference(table), text,
                             CAST.reference_text(table), name,
                             LOCATIONS.reference_text(table),
                             party=PARTY.prompt_text(table))
    try:
        updated = ask_llm(_memory_system_prompt(name), prompt, [],
                          model=_model_for(table), on_usage=_meter(table)).strip()
        if not updated:
            return  # degenerate response — keep the existing memory, don't wipe it
        fold = _split_fold_in_response(updated)
        memory_text = _cap_words(fold.memory, GAME_CONTEXT_MAX_WORDS)
        with rt.game_context_lock:
            context.write_text(memory_text + "\n", encoding="utf-8")
        if fold.bond:
            familiars.write_bond(familiar, fold.bond)
        if fold.events:
            _append_timeline(table, rt, fold.events)
        if fold.cast:
            CAST.apply_updates(table, rt, fold.cast, day)
        if fold.locations:
            LOCATIONS.apply_updates(table, rt, fold.locations, day)
    except Exception:
        app.logger.exception("could not update game-context.md")


def _prior_session_turns(table) -> list[tuple[str, str]]:
    """The (question, answer) turns from the most recent *earlier* session log.
    Prefers the newest log before today so a fresh session recaps the last one;
    if today's is the only log (a resumed session), falls back to that."""
    try:
        # date-only names, e.g. 2026-08-21.log -- excludes the newer
        # <date>-timeline.log event files, which "*.log" would also match
        logs = sorted(table.sessions_dir.glob("????-??-??.log"))
    except OSError:
        return []
    if not logs:
        return []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prior = [p for p in logs if p.stem != today] or logs
    path = prior[-1]
    turns: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            turns.append((rec["q"], rec["a"]))
        except (ValueError, KeyError):
            continue
    return turns[-RECAP_MAX_TURNS:]


@app.post("/api/t/<slug>/recap")
@require_member()
@auth.require_json
def api_recap():
    """Recap the previous session in the familiar's own voice. Feeds the prior
    session's logged Q&A back as conversation history and asks for a summary;
    the browser speaks the returned text. 204 when there's nothing to recap."""
    turns = _prior_session_turns(g.table)
    if not turns:
        return ("", 204)
    instruction = (
        "The players have just sat back down for a new session. In your own "
        "voice, briefly recap what happened last time — the key events, "
        "decisions, and where things were left — as if reminding them. Keep it "
        "to a few sentences."
    )
    try:
        recap = ask_llm(build_system_prompt(g.table), instruction, turns,
                        model=_model_for(g.table), on_usage=_meter(g.table))
    except Exception as exc:
        app.logger.exception("recap failed")
        return jsonify({"error": str(exc)}), 502
    return jsonify({"recap": recap})


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _amendments_path(table, day: str) -> Path:
    return table.sessions_dir / f"{day}-amendments.log"


def _load_amendments(table, day: str) -> dict:
    """event_index -> ordered list of {"text", "by", "at", "roll"?} amendments
    for that date, oldest first."""
    path = _amendments_path(table, day)
    by_index: dict = {}
    if not path.exists():
        return by_index
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            by_index.setdefault(int(rec["event_index"]), []).append(rec)
        except (ValueError, KeyError, TypeError):
            continue
    return by_index


def _append_amendment(table, rt, day: str, event_index: int, text: str, by: str,
                      tags: dict | None = None) -> None:
    """Append one post-game correction. Never touches the original timeline
    line -- amendments live in a parallel append-only file, joined back in by
    _resolve_timeline_events() at read time. Multiple players amending the
    same event just accumulate here; nothing is ever overwritten.

    `tags` may map any of roll/rest/weather to a value *or to None*. A key
    present with None means "clear this" -- which is the correction that
    actually matters here, since the familiar will sometimes hear a party
    wondering aloud about a long rest and record one they never took.
    """
    rec = {
        "event_index": event_index,
        "text": text,
        "by": by,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    for kind, value in (tags or {}).items():
        rec[kind] = value          # None is meaningful: it clears
    table.sessions_dir.mkdir(parents=True, exist_ok=True)
    with rt.timeline_lock:
        with _amendments_path(table, day).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    # After the durable write, never before: a correction that reached the file
    # but not the index is a search miss, one that reached neither is a lie.
    _reindex_date(table, day)


def _resolve_timeline_events(table, date: str) -> list:
    """The event timeline for one date, joined with any post-game amendments.
    This is the single place "current" text/roll gets decided (latest
    amendment wins, else the original capture) -- the timeline viewer, the
    amend endpoint's response, and /api/stats all go through this so they can
    never disagree about what's current."""
    path = _timeline_path(table, date)
    if not path.exists():
        return []
    amendments = _load_amendments(table, date)
    events = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        amends = amendments.get(index, [])
        # Key *presence*, not truthiness. `if a.get("roll")` could only ever set
        # a tag, never remove one -- and removing a rest the familiar imagined
        # is the whole reason these are amendable. An explicit null clears.
        current = {kind: rec.get(kind) for kind in TIMELINE_TAGS}
        for a in amends:
            for kind in TIMELINE_TAGS:
                if kind in a:
                    current[kind] = a[kind]
        events.append({
            "id": rec.get("id") or f"{date}-{index:04d}",
            "t": rec.get("t"),
            "original": rec.get("event", ""),
            "current": amends[-1]["text"] if amends else rec.get("event", ""),
            "roll": current["roll"],
            "rest": current["rest"],
            "weather": current["weather"],
            "amendments": [
                {"text": a.get("text", ""), "by": a.get("by", ""), "at": a.get("at", "")}
                for a in amends
            ],
        })
    return events


def _session_dates(table) -> list[str]:
    """Every date this table has a recorded timeline for, oldest first.

    Three call sites globbed for this independently before the in-game clock
    made it four; the clock has to walk them in order, so it was worth having
    one definition of what "in order" means.
    """
    try:
        return sorted(p.name.removesuffix("-timeline.log")
                      for p in table.sessions_dir.glob("*-timeline.log"))
    except OSError:
        return []


@app.get("/api/t/<slug>/sessions")
@require_member()
def api_sessions():
    """Dates with a recorded event timeline (sessions/<date>-timeline.log),
    newest first -- for the timeline.html viewer's date picker."""
    return jsonify({"dates": sorted(_session_dates(g.table), reverse=True)})


@app.get("/api/t/<slug>/sessions/<date>/timeline")
@require_member()
def api_session_timeline(date):
    """The event timeline for one date, oldest first, with amendments resolved
    (see _resolve_timeline_events()). 400 on a malformed date; empty list if
    that date has none."""
    if not _DATE_RE.match(date):
        return jsonify({"error": "invalid date"}), 400
    events = _resolve_timeline_events(g.table, date)
    # Which in-game day each event falls on. It depends on every *earlier*
    # session's rests, not just this one's, so it cannot be computed from the
    # events above -- but the viewer wants to draw day dividers without making
    # a second request for it.
    event_days, _days = _campaign_days(g.table)
    for event in events:
        event["day"] = event_days.get(event["id"])
    return jsonify({"date": date, "events": events})


def _parse_roll_payload(roll):
    """Validate an optional {"character", "outcome"} roll dict from a request
    body. Returns (roll_or_None, error_message_or_None)."""
    if roll is None:
        return None, None
    if not isinstance(roll, dict):
        return None, "invalid roll"
    character = str(roll.get("character") or "").strip()
    outcome = str(roll.get("outcome") or "").strip().lower()
    if not character or outcome not in ROLL_OUTCOMES:
        return None, "invalid roll"
    return {"character": character, "outcome": outcome}, None


def _parse_rest_payload(rest):
    if rest is None:
        return None, None
    value = str(rest).strip().lower()
    if value not in REST_TYPES:
        return None, f"invalid rest (expected one of {', '.join(sorted(REST_TYPES))})"
    return value, None


def _parse_weather_payload(weather):
    if weather is None:
        return None, None
    value = " ".join(str(weather).split())[:WEATHER_MAX_CHARS].strip()
    return (value or None), None


_TAG_PARSERS = {
    "roll": _parse_roll_payload,
    "rest": _parse_rest_payload,
    "weather": _parse_weather_payload,
}


def _parse_tag_payloads(payload: dict):
    """Read the tag corrections out of an amend request body.

    Only keys actually *present* are returned, so omitting `rest` leaves the
    existing one alone while sending `"rest": null` removes it. That difference
    is the entire correction story, so it lives in one place rather than being
    re-derived at each call site.
    """
    tags: dict = {}
    for kind, parser in _TAG_PARSERS.items():
        if kind not in payload:
            continue
        value, err = parser(payload[kind])
        if err:
            return None, err
        tags[kind] = value      # may be None, meaning "clear it"
    return tags, None


@app.post("/api/t/<slug>/sessions/<date>/timeline/<event_id>/amend")
@require_member()
@auth.require_json
def api_timeline_amend(date, event_id):
    """Append a post-game correction to one timeline event. Body:
    {"text": "...", "roll": {...} | null, "rest": "long" | null,
     "weather": "..." | null}. A tag key that is *absent* is left alone; a key
    sent as null is cleared. Never rewrites the original event -- see _append_amendment(). Returns the
    event's fully resolved shape (same as the list endpoint's per-event
    object) so the caller can patch its UI from the response.

    Who made the correction comes from the signed-in member, not the request
    body. It used to be a `by` field the browser filled in from a name typed
    into localStorage, which meant the attribution on a permanent record was
    whatever the client felt like sending."""
    if not _DATE_RE.match(date):
        return jsonify({"error": "invalid date"}), 400
    if not event_id.startswith(f"{date}-"):
        return jsonify({"error": "event does not belong to that date"}), 400
    try:
        index = int(event_id[len(date) + 1 :])
    except ValueError:
        return jsonify({"error": "invalid event id"}), 400

    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    tags, err = _parse_tag_payloads(payload)
    if err:
        return jsonify({"error": err}), 400

    table, rt = g.table, g.rt
    by = g.member["display_name"]
    events = _resolve_timeline_events(table, date)
    if not any(e["id"] == event_id for e in events):
        return jsonify({"error": "unknown event"}), 404

    _append_amendment(table, rt, date, index, text, by, tags)
    updated = next(e for e in _resolve_timeline_events(table, date) if e["id"] == event_id)
    return jsonify(updated)


@app.get("/api/t/<slug>/cast")
@require_member()
def api_cast():
    """The auto-maintained NPC cast, newest-touched first."""
    return jsonify({"npcs": CAST.listing(g.table)})


@app.post("/api/t/<slug>/cast/<npc_id>/amend")
@require_member()
@auth.require_json
def api_cast_amend(npc_id):
    """Append a post-game correction to one NPC's description. Body:
    {"text": "..."}; the amender is the signed-in member. The `auto` field is
    never overwritten by this -- see Roster.apply_updates(), which always
    preserves `amendments` across every later auto-update."""
    return _amend_roster(CAST, npc_id)


@app.get("/api/t/<slug>/locations")
@require_member()
def api_locations():
    """The auto-maintained list of places, newest-touched first. The same
    shape as /cast down to the field names, so static/roster.js renders both."""
    return jsonify({"places": LOCATIONS.listing(g.table)})


@app.post("/api/t/<slug>/locations/<place_id>/amend")
@require_member()
@auth.require_json
def api_location_amend(place_id):
    """Append a post-game correction to one place's description. As
    api_cast_amend(), and for the same reasons."""
    return _amend_roster(LOCATIONS, place_id)


def _amend_roster(roster, entry_id: str):
    """The body shared by both amend endpoints. Kept as a plain function
    rather than a route factory so the two routes stay greppable."""
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    result = roster.amend(g.table, g.rt, entry_id, text, g.member["display_name"])
    if result is None:
        return jsonify({"error": f"unknown {roster.noun}"}), 404
    return jsonify(result)


@app.get("/api/t/<slug>/search")
@require_member()
def api_search():
    """Search everything this table has recorded: timeline events and their
    corrections, NPCs, places, and questions anyone asked the familiar.

    Query params: `q` (required), `kind` (optional filter, one of
    db.SEARCH_KINDS), `limit` (default 40, capped in db.search).

    Scoped to g.table by the database, not by filtering afterwards -- the
    index is the one place in the schema where every tenant's prose sits in a
    single table, so the WHERE clause is the tenancy boundary and belongs
    where it cannot be forgotten.

    Returns 200 with an empty list for a query that matches nothing OR that
    contains nothing searchable; an empty search box is not an error.
    """
    query = (request.args.get("q") or "").strip()
    kind = (request.args.get("kind") or "").strip()
    if kind and kind not in database.SEARCH_KINDS:
        return jsonify({"error": "unknown kind"}), 400
    try:
        limit = int(request.args.get("limit") or 40)
    except ValueError:
        return jsonify({"error": "limit must be a number"}), 400

    results = [dict(r) for r in db.search(g.table.id, query, kind, limit)]
    return jsonify({
        "query": query,
        "kind": kind,
        "results": results,
        "counts": db.search_counts(g.table.id, query),
        "marks": [database.MARK_START, database.MARK_END],
    })


# ------------------------------------------------- taking it away again ---
#
# Export and deletion are not features that get added when somebody asks; they
# are the price of holding a stranger's evening on a disk. Both are here before
# the first stranger, on purpose.


def _is_owner() -> bool:
    """Whether this request is the person who owns (and pays for) the table."""
    user = auth.current_user(db)
    return bool(user) and user["id"] == g.table.owner_id


def _table_archive(table) -> io.BytesIO:
    """Everything a table has recorded, as a zip, built in memory.

    In memory because a table's whole history is a few hundred kilobytes of
    prose -- the entire point of the design is that nothing here is big. If
    that ever stops being true, this becomes a streamed response, and the
    tests around it will not have to change.

    Contains the files and nothing else: the database holds only the table's
    name, its members' display names and a count of tokens spent, all of which
    are either in the files already or are ours rather than theirs.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(table.dir.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(table.dir).as_posix())
        z.writestr("README.txt",
                   f"Everything {table.name} recorded, exported "
                   f"{datetime.now(timezone.utc):%Y-%m-%d}.\n\n"
                   "sessions/<date>.log            questions asked and answers given\n"
                   "sessions/<date>-timeline.log   what happened, one event per line\n"
                   "sessions/<date>-amendments.log corrections, and who made them\n"
                   "cast.json / locations.json     who you met, and where you went\n"
                   "game-context.md                the running memory\n"
                   "campaign.md / persona.md       what you wrote yourselves\n"
                   "familiars/<id>/                each familiar's persona and bond\n\n"
                   "All of it is plain text or JSON, readable without this service.\n")
    buf.seek(0)
    return buf


@app.get("/api/t/<slug>/export")
@require_member()
def api_export():
    """Download everything this table has recorded, as a zip.

    Owner or DM: the two people who could already read all of it in the
    browser. Not every member, because an archive is trivially copied and a
    player leaving with the whole campaign is a different act from a player
    reading it a page at a time.
    """
    if not (_is_owner() or g.member["role"] == "dm"):
        return jsonify({"error": "only the table's owner or DM can export it"}), 403
    buf = _table_archive(g.table)
    name = f"{g.table.slug}-{datetime.now(timezone.utc):%Y-%m-%d}.zip"
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name=name)


def delete_table_completely(table) -> None:
    """Erase a table: its rows, its search index, and its files.

    The order matters. Rows first, so a failure half-way leaves a table that
    cannot be reached rather than one that is reachable and empty; then the
    directory. db.delete_table() clears the search index itself, because the
    index takes no foreign key and a cascade would leave a stranger's prose
    sitting in a table nobody can open.
    """
    db.delete_table(table.id)
    shutil.rmtree(table.dir, ignore_errors=True)
    registry.drop(table.id)


@app.post("/api/t/<slug>/delete")
@require_member()
@auth.require_json
def api_delete_table():
    """Delete this table and everything in it. Owner only, and irreversible.

    Requires the table's slug echoed back in the body. Not a confirmation
    dialog's job alone: this is the one call in the service that destroys
    somebody else's evenings, and it should be hard to make by accident from a
    script as well as from a browser.
    """
    if not _is_owner():
        return jsonify({"error": "only the table's owner can delete it"}), 403
    confirm = ((request.get_json(silent=True) or {}).get("confirm") or "").strip()
    if confirm != g.table.slug:
        return jsonify({"error": f"type the table's name ({g.table.slug}) to confirm"}), 400
    delete_table_completely(g.table)
    return jsonify({"ok": True})


@app.post("/api/t/<slug>/leave")
@require_member()
@auth.require_json
def api_leave_table():
    """Give up your own membership of somebody else's table.

    Your corrections stay on the record under the name you made them with.
    That is not an oversight: an amendment is a statement about what happened
    at a table, and the table keeps its own history. What goes is your way back
    in.

    The owner cannot leave -- there would be nobody to answer for the table.
    They delete it instead.
    """
    if _is_owner():
        return jsonify({
            "error": "you own this table, so you cannot leave it — delete it instead",
        }), 400
    if not db.remove_member(g.table.id, g.member["id"]):
        # remove_member only removes players. A DM walking out would leave a
        # table nobody can administer, so say so rather than returning a
        # cheerful ok that removed nothing.
        return jsonify({
            "error": "a table cannot be left without a DM — hand the game over "
                     "or ask the owner to delete the table",
        }), 400
    response = jsonify({"ok": True})
    auth.clear_cookie(response, auth.member_cookie(g.table.id))
    return response


@app.post("/api/account/delete")
@auth.require_json
def api_delete_account():
    """Delete your account, and every table you own with it.

    Tables you merely play at are left alone -- they are not yours to destroy.
    Requires your password, because a signed-in browser left open on a table is
    a normal way for this app to be used.
    """
    user = auth.current_user(db)
    if user is None:
        return jsonify({"error": "sign in first"}), 401
    password = (request.get_json(silent=True) or {}).get("password") or ""
    if db.check_login(user["email"], password) is None:
        return jsonify({"error": "that password is not right"}), 403

    owned = [Table(row, DATA_DIR, BASE) for row in db.tables_for_user(user["id"])
             if row["owner_id"] == user["id"]]
    db.delete_user(user["id"])
    for table in owned:
        shutil.rmtree(table.dir, ignore_errors=True)
        registry.drop(table.id)
    response = jsonify({"ok": True, "tables_deleted": len(owned)})
    auth.clear_cookie(response, auth.USER_COOKIE)
    return response


def _campaign_days(table) -> tuple[dict, list]:
    """The in-game calendar, derived from long rests. Nothing is stored.

    Returns ({event_id: day_number}, [day summaries]).

    A long rest ends a day: the event carrying the tag belongs to the day the
    party has just spent, and everything after it is the morning of the next
    one. Short rests are recorded but do not move the clock.

    Derived rather than stored, exactly like api_stats() -- so amending away a
    rest the familiar imagined (a group saying "should we long rest?" and then
    not) shifts the calendar with no separate invalidation step to get wrong.
    It costs one pass over every timeline per call, which is what api_stats
    already does; at a home game's scale that is nothing, and if it ever stops
    being nothing both want the same cache.

    The day is a plain counter. Mapping it onto a real calendar -- Harptos or
    otherwise -- is a later reading of the same numbers, and needs no change to
    anything recorded here.
    """
    event_days: dict[str, int] = {}
    days: list[dict] = []
    day = 1
    current = {"day": day, "sessions": [], "weather": [], "events": 0,
               "ended_by": None, "ended_at": None}

    for date in _session_dates(table):
        for event in _resolve_timeline_events(table, date):
            event_days[event["id"]] = current["day"]
            current["events"] += 1
            if date not in current["sessions"]:
                current["sessions"].append(date)
            weather = event.get("weather")
            if weather and weather not in current["weather"]:
                current["weather"].append(weather)
            if event.get("rest") == "long":
                # This event closes the day; the next one opens a new morning.
                current["ended_by"] = event["id"]
                current["ended_at"] = event.get("t")
                days.append(current)
                day += 1
                current = {"day": day, "sessions": [], "weather": [], "events": 0,
                           "ended_by": None, "ended_at": None}

    # The day in progress always counts, even with nothing recorded on it yet.
    # A campaign that has never rested is on day 1 rather than on no day at
    # all; and a party whose last recorded act was making camp is on the
    # morning *after* that rest, not still in the evening before it.
    days.append(current)
    return event_days, days


@app.get("/api/t/<slug>/calendar")
@require_member()
def api_calendar():
    """The in-game clock: how many days the party has lived through, and what
    the weather did. Computed fresh from the amendment-resolved timeline on
    every request -- see _campaign_days()."""
    _event_days, days = _campaign_days(g.table)
    return jsonify({
        "days": days,
        "current_day": days[-1]["day"] if days else 1,
        "rests_recorded": sum(1 for d in days if d["ended_by"]),
    })


@app.get("/api/t/<slug>/stats")
@require_member()
def api_stats():
    """Success/failure/crit/nat1 tallies, joined to the party at read time.

    Computed fresh on every request by scanning every date's resolved
    (amendment-aware) timeline, so a post-game correction shows up in the
    numbers with no invalidation step to get wrong. The join to the party is
    read-time for the same reason, and for one more: the roster is often filled
    in *after* the session that produced the rolls, and a write-time check
    would have thrown those rolls away.

    Two blocks come back rather than one dictionary keyed on whatever the model
    typed:

    `characters` is the party, every one of them, **including those who have
    never rolled** -- a character at zero is a fact about the campaign, and the
    old shape could not express it.

    Deleting a character does not un-throw their dice: match() stops finding
    them, so their rolls reappear under `unplaced`, which is the honest answer
    -- something rolled and we no longer know who.

    `unplaced` is every roll name that matches nobody. Its own block instead of
    a fifth "character", because "Seraphine the Paladin" sitting in the table next
    to "Seraphine" looks like two people and is really one mis-transcription. The
    fix is an alias, which is a human decision -- see Party.match(), which is
    exact on purpose.
    """
    table = g.table
    tallies: dict = {}
    total = 0
    for date in _session_dates(table):
        for event in _resolve_timeline_events(table, date):
            roll = event.get("roll")
            if not roll:
                continue
            tally = tallies.setdefault(roll["character"], {o: 0 for o in ROLL_OUTCOMES})
            tally[roll["outcome"]] += 1
            total += 1

    def _empty():
        return {outcome: 0 for outcome in ROLL_OUTCOMES}

    placed: dict = {}
    unplaced: dict = {}
    for name, tally in tallies.items():
        pc_id = PARTY.match(table, name)
        target = placed.setdefault(pc_id, _empty()) if pc_id else unplaced.setdefault(name, _empty())
        for outcome, count in tally.items():
            target[outcome] += count

    characters = [
        {"id": entry["id"], "name": entry["name"], "retired": entry["retired"],
         "tally": placed.get(entry["id"], _empty())}
        for entry in PARTY.listing(table)
    ]
    return jsonify({
        "characters": characters,
        "unplaced": [{"name": name, "tally": tally}
                     for name, tally in sorted(unplaced.items())],
        "total_events_with_rolls": total,
    })


def _fold_in_from_events_prompt(existing: str, campaign: str, cast_reference: str,
                                events_text: str, name: str = "the familiar",
                                location_reference: str = "", *, party: str = "") -> str:
    """Like _fold_in_prompt(), but replays a list of ALREADY-CONFIRMED session
    events instead of raw transcript -- used only by /api/sessions/<date>/
    reprocess. No '## New events' section: the timeline being replayed is the
    source, not something to regenerate. Cast and locations ARE regenerated,
    because both are descriptions built up from those events and a corrected
    event should correct them."""
    return (
        f"Update {name}'s running session memory by replaying a list of "
        "already-confirmed session events (not raw transcript -- treat each "
        "as settled fact, nothing to filter for small talk or noise). Below: "
        "the cast for context, the memory as it currently stands, the NPC "
        "cast list as it currently stands, and the ordered list of events for "
        "one session.\n\n"
        f"Keep the WHOLE memory under {GAME_CONTEXT_MAX_WORDS} words. Output "
        "exactly these five headed sections, in this priority order if you "
        "must cut something to stay under the limit (the last two sections are "
        "permanent records, kept OUTSIDE the word limit — never shorten or "
        "drop anything from either of them):\n\n"
        "## Party & relationships\n"
        f"{name}'s standing with each party member and the dynamics between "
        "them. Highest priority — change only when the events show something "
        "new or different, otherwise carry it forward unchanged. The party "
        f"list below is settled fact and is already given to {name} "
        "separately: do NOT restate who anybody is here. This section is only "
        f"about how {name} feels about them.\n\n"
        "## Ongoing threads\n"
        "Unresolved quests, promises, dangers, as a short bullet list. Remove "
        "a thread once it's resolved. Second priority.\n\n"
        "## Recent developments\n"
        "The freshest few events, most recent first. Lowest priority — this "
        "is the section to shorten or drop entries from first.\n\n"
        "## Cast updates\n"
        "NPCs newly introduced or meaningfully changed by THIS session's "
        "events only — one bullet per NPC, formatted exactly as \"Name: "
        "current full description\" (skip anyone with nothing new to add).\n\n"
        "## Location updates\n"
        "Named places the party visited, arrived at, learned about or that "
        "changed in THIS session's events only — same format, one bullet per "
        "place as \"Name: current full description\". A place earns a bullet "
        "when it has a name the party would use again; skip anywhere with "
        "nothing new to add.\n\n"
        "Both \"## Cast updates\" and \"## Location updates\" must be written "
        "exactly as shown (two '#' characters, like the three headings above "
        "them) — do not use a \"---\" divider for either, and do not echo any "
        "of the labels above back into your answer.\n\n"
        "Only state what the events or existing memory actually support; "
        "never invent facts.\n\n"
        f"=== the party (settled fact) ===\n{party or '(nobody recorded yet)'}\n\n"
        f"=== campaign notes (reference only) ===\n{campaign}\n\n"
        f"=== current memory ===\n{existing or '(empty — first update)'}\n\n"
        f"=== NPC cast list so far (reference only) ===\n{cast_reference or '(empty — first update)'}\n\n"
        f"=== places known so far (reference only) ===\n{location_reference or '(empty — first update)'}\n\n"
        f"=== session events (settled, in order) ===\n{events_text}"
    )


@app.post("/api/t/<slug>/sessions/<date>/reprocess")
@require_member(role="dm")
@auth.require_json
def api_reprocess(date):
    """Regenerate game-context.md (and cast.json's auto text) from this date
    forward through today, using each timeline event's CURRENT (amended) text
    rather than raw transcript. Because game-context.md is a cumulative,
    capped rewrite, correcting one date's contribution requires replaying
    every LATER date too, in order -- each later date's start-of-day snapshot
    is re-anchored to the previous date's corrected result as we go, so a
    later, separate reprocess of just one of those dates starts from an
    already-correct anchor. Explicitly user-triggered (a button after a
    review pass), never automatic per amendment, to keep LLM cost bounded and
    predictable. Accepted risk, not engineered around: a live fold-in racing a
    reprocess could be clobbered -- fine at this home-game scale, but don't
    reprocess mid-session."""
    if not _DATE_RE.match(date):
        return jsonify({"error": "invalid date"}), 400
    table, rt = g.table, g.rt
    cascade = [d for d in _session_dates(table) if d >= date]
    if not cascade:
        return jsonify({"error": "no session data on or after that date"}), 404

    first = cascade[0]
    start_path = table.sessions_dir / f"{first}-context-start.md"
    memory = start_path.read_text(encoding="utf-8") if start_path.exists() else ""
    roster = _campaign_reference(table)
    familiars = table.familiars
    familiar = familiars.active()
    name = familiar.name if familiar else "the familiar"

    try:
        for i, day in enumerate(cascade):
            events = _resolve_timeline_events(table, day)
            if not events:
                continue
            events_text = "\n".join(f"- {e['current']}" for e in events)
            prompt = _fold_in_from_events_prompt(memory, roster, CAST.reference_text(table),
                                                 events_text, name,
                                                 LOCATIONS.reference_text(table),
                                                 party=PARTY.prompt_text(table))
            updated = ask_llm(_memory_system_prompt(name), prompt, [],
                              model=_model_for(table), on_usage=_meter(table)).strip()
            if not updated:
                continue
            fold = _split_fold_in_response(updated)
            memory = _cap_words(fold.memory, GAME_CONTEXT_MAX_WORDS)
            if fold.bond:
                familiars.write_bond(familiar, fold.bond)
            if fold.cast:
                CAST.apply_updates(table, rt, fold.cast, day)
            if fold.locations:
                LOCATIONS.apply_updates(table, rt, fold.locations, day)
            if i + 1 < len(cascade):
                (table.sessions_dir / f"{cascade[i + 1]}-context-start.md").write_text(
                    memory, encoding="utf-8")
    except Exception as exc:
        app.logger.exception("reprocess failed")
        return jsonify({"error": str(exc)}), 502

    with rt.game_context_lock:
        table.game_context_file.write_text(memory + "\n", encoding="utf-8")

    return jsonify({"ok": True, "reprocessed_dates": cascade})


# ── Body bus ────────────────────────────────────────────────────────────
# One brain, many bodies. Every body — the on-screen cat, a second screen,
# the animatronic puppet later — holds a WebSocket to /ws/state and reacts to
# the same five states (asleep, idle, listening, thinking, speaking).
#
# Messages are a JSON envelope keyed by `type`, so the protocol can grow
# without breaking older bodies (which ignore types they don't understand):
#
#     {"type": "state", "state": "thinking"}     the state machine moved
#     {"type": "viseme", "level": 0.7}           (future) jaw-flap amplitude
#
# Whoever has the mic (usually the listening screen) drives; the server just
# remembers the latest state and fans every message out to the *other* bodies.
#
# The bus is per table. Bodies, the lock and the current state all live on the
# table's TableRuntime -- one shared set of them would have every table's
# puppet twitching to every other table's conversation.


class _Body:
    """One connected body on the bus: its socket plus an outbound queue drained
    by a dedicated writer thread. Broadcasts enqueue (never block on the socket),
    so one slow or wedged body — a laggy screen, a puppet on flaky WiFi — can't
    hold up state/viseme updates to every other body."""

    def __init__(self, ws, may_drive: bool = True):
        self.ws = ws
        # Whether this body drives the bus or only listens. Kept here as well
        # as on the seat because it is needed after the connection ends, when
        # the seat is gone -- see settle_bus.
        self.may_drive = may_drive
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._writer = threading.Thread(target=self._run, daemon=True)
        self._writer.start()

    def send(self, data: str) -> None:
        """Queue a message; if this body has fallen far behind, drop its oldest
        pending message rather than grow memory without bound (state is
        last-value-wins, so a stale queued frame is worth losing)."""
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            try:
                self._queue.get_nowait()   # drop oldest, make room
                self._queue.put_nowait(data)
            except (queue.Empty, queue.Full):
                pass

    def _run(self) -> None:
        while True:
            data = self._queue.get()
            if data is None:               # sentinel from close()
                return
            try:
                self.ws.send(data)
            except Exception:
                try:
                    self.ws.close()        # unstick the receive loop so it cleans up
                except Exception:
                    pass
                return

    def close(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(None)
            except (queue.Empty, queue.Full):
                pass


class BusSeat(NamedTuple):
    """Who is on the bus, and what they are allowed to do there."""
    rt: object
    device: object = None   # the devices row, when a puppet is connecting
    may_drive: bool = True  # whether their messages are fanned out to others


def bearer_token() -> str:
    """The `Authorization: Bearer <token>` value on this request, or ""."""
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def ws_seat(slug: str):
    """The seat for a bus connection, or None if this caller may not join.

    Two credentials, because two kinds of body connect here:

    * a **browser** sends the member cookie it already has -- the same one
      every /api call uses, so a screen needs nothing extra;
    * a **puppet** sends `Authorization: Bearer <device token>`, because a cat
      on a shelf cannot follow an invite link and type its name. Browsers
      cannot set headers on a WebSocket, and a device has no cookie jar, so
      the two paths never overlap.

    A device gets `may_drive=False`. It listens; it does not broadcast. The
    reasoning is where the credential lives: a device token sits in a file
    inside a physical object in somebody's house, and if that object is taken,
    the worst it should buy is watching a cat's jaw move -- not the ability to
    push arbitrary states at every other body at that table. Nothing needs the
    other direction yet; when something does (a button on the puppet that
    wakes the familiar), this is the line that changes, and it is one line.

    Split out from ws_state because flask-sock's decorator does not leave the
    view function behind to call, and an authorization decision that cannot be
    tested is one you find out about the hard way.
    """
    row = db.table_by_slug(slug)
    if row is None:
        return None
    if auth.resolve_member(db, row) is not None:
        return BusSeat(get_runtime(row["id"]))
    device = db.device_by_token(bearer_token())
    # The table check is the tenancy boundary, not a formality: a token minted
    # at one table must not open another's bus.
    if device is None or device["table_id"] != row["id"]:
        return None
    db.touch_device(device["id"])
    return BusSeat(get_runtime(row["id"]), device=device, may_drive=False)


# What the bus holds when the last screen goes away.
#
# `asleep` rather than leaving the last state alone, and this was learned from
# a puppet that flapped its jaw for fourteen minutes. A tablet broadcast
# `speaking`, then vanished mid-utterance -- backgrounded, closed, or off the
# wifi -- and never sent what came next. The server keeps the last state it saw
# and hands it to everyone, so every body went on faithfully holding a pose
# that had stopped being true, with no sound to go with it because nothing was
# speaking. Nothing was broken; the bus was simply asserting something stale
# with great confidence.
#
# `speaking` and `thinking` are things a familiar is *doing*, and a body cannot
# be doing them when nobody is there. So when the last driver disconnects the
# bus settles, which is also the honest state: no screen, no session, asleep.
REST_STATE = "asleep"


def settle_bus(rt) -> bool:
    """Put the bus to rest if no driver is left on it. Returns whether it did.

    Split out from ws_state for the same reason ws_seat was: flask-sock leaves
    no view function behind to call, and this rule is far easier to get subtly
    wrong (a puppet is a body but not a driver; two screens means one leaving
    changes nothing) than it is to read.
    """
    with rt.bodies_lock:
        drivers = any(getattr(body, "may_drive", False) for body in rt.bodies)
    if drivers or rt.current_state == REST_STATE:
        return False
    rt.current_state = REST_STATE
    rt.broadcast(json.dumps({"type": "state", "state": REST_STATE}))
    return True


@sock.route("/t/<slug>/ws/state")
def ws_state(ws, slug):
    """One table's body bus.

    Authenticated on connect, and this is not a formality. Before Phase 1 the
    only thing in front of this socket was Caddy's site-wide basic_auth; with
    that gone, an unauthenticated bus would let anyone connect and broadcast
    arbitrary state to every body at every table. The membership cookie the
    browser already holds is the credential -- the same one every /api call
    uses -- so a connecting screen needs nothing extra.

    A paired puppet arrives with a device token instead.
    """
    seat = ws_seat(slug)
    if seat is None:
        ws.close()
        return
    rt = seat.rt
    body = _Body(ws, may_drive=seat.may_drive)
    # Snapshot first: a body that connects mid-session snaps to the current pose
    # before it can receive any later broadcast.
    body.send(json.dumps({"type": "state", "state": rt.current_state}))
    rt.add_body(body)
    try:
        while True:
            raw = ws.receive()  # blocks; returns None when the body disconnects
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not seat.may_drive:
                # A puppet is a body, not a driver. See ws_seat.
                continue
            if msg.get("type") == "state" and isinstance(msg.get("state"), str):
                rt.current_state = msg["state"]
            rt.broadcast(json.dumps(msg), source=body)
    finally:
        rt.remove_body(body)
        body.close()
        settle_bus(rt)


# ── Accounts, tables and invitations ────────────────────────────────────
# Small, hand-rolled and deliberately boring. A DM signs up with an email and a
# password; players never sign up at all -- they follow an invite link, type a
# name, and are remembered by a cookie. See auth.py for the cookie mechanics.


# Who may create an account. Three states, from one setting:
#
#   SIGNUP_CODE unset, no users yet   "open"   -- the first account, bootstrap
#   SIGNUP_CODE=<something>           "code"   -- an invitation code is required
#   SIGNUP_CODE=open                  "open"   -- deliberately open to anyone
#   SIGNUP_CODE unset, users exist    "closed" -- nobody new, until one is set
#
# The default is the interesting part. Before this, signup was open to whoever
# could reach the site, and the only thing in front of it was a shared password
# in the reverse proxy that the deployment notes tell you to delete. Removing
# that block -- the documented next step -- silently opened account creation to
# the internet, and every table a stranger made would spend the operator's own
# LLM budget.
#
# So the safe state is the one you get by forgetting: a deployment with users
# and no configured code takes no new accounts at all. The single exception is
# the *first* account, because a fresh install has nobody who could have issued
# a code, and a getting-started guide that begins "edit .env and restart" for
# something this small is a guide people abandon. That window is one account
# wide and closes the moment it is used.
def signup_mode() -> str:
    """"open", "code" or "closed" -- see the table above."""
    configured = (os.environ.get("SIGNUP_CODE") or "").strip()
    if configured.casefold() == "open":
        return "open"
    if configured:
        return "code"
    return "closed" if db.any_users() else "open"


def signup_code_ok(supplied: str) -> bool:
    """Whether `supplied` is the configured invitation code.

    `compare_digest` because this is a shared secret an operator picked, which
    means it may be short and word-shaped rather than random. Case and
    surrounding space are forgiven: it gets read off a screen, said aloud, or
    pasted out of a message, and refusing "Dragons-2026 " for the space would
    be the service being right and useless.
    """
    configured = (os.environ.get("SIGNUP_CODE") or "").strip()
    # Compared as bytes: compare_digest raises TypeError on non-ASCII str, and
    # a code with an accent (or a pasted curly quote) must be a 403, not a 500.
    return secrets.compare_digest((supplied or "").strip().casefold().encode(),
                                  configured.casefold().encode())


def _client() -> str:
    """The address a rate limit is keyed on. See the ProxyFix note above."""
    return request.remote_addr or "unknown"


def _slow_down(*checks):
    """A 429 if any (limit, key) pair is over, else None. Retry-After is the
    longest wait among them, so a client that honours it is not refused again."""
    wait = max((limit.retry_after(key) for limit, key in checks), default=0)
    if not wait:
        return None
    response = jsonify({"error": "too many attempts — wait a few minutes and try again"})
    response.headers["Retry-After"] = str(wait)
    return response, 429


@app.get("/api/signup")
def api_signup_policy():
    """What the signup form should render. Deliberately public.

    It says whether a code is needed, never what the code is. The alternative
    -- a form that always shows the field and fails after you have typed your
    email and chosen a password -- spends somebody's goodwill to hide a fact
    they can discover by pressing the button once.
    """
    return jsonify({"mode": signup_mode()})


@app.post("/api/signup")
@auth.require_json
def api_signup():
    payload = request.get_json(silent=True) or {}
    mode = signup_mode()
    # Every attempt counts, not just failures: here success is the expensive
    # outcome -- each account can make tables that spend the operator's budget.
    # A closed door is exempt; there is nothing behind it to guess at.
    if mode != "closed":
        denied = _slow_down((ratelimit.SIGNUP_ATTEMPTS_PER_IP, _client()))
        if denied:
            return denied
        ratelimit.SIGNUP_ATTEMPTS_PER_IP.record(_client())
    if mode == "closed":
        return jsonify({
            "error": "this familiar is not taking new accounts at the moment",
        }), 403
    if mode == "code" and not signup_code_ok(payload.get("code", "")):
        # The same message whether the code was wrong or missing: they are the
        # same fact from outside, and distinguishing them is a way to probe.
        return jsonify({"error": "that invitation code is not right"}), 403
    try:
        user_id = db.create_user(payload.get("email", ""), payload.get("password", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    response = jsonify({"ok": True})
    return auth.set_cookie(response, auth.USER_COOKIE, user_id, "user")


@app.post("/api/login")
@auth.require_json
def api_login():
    payload = request.get_json(silent=True) or {}
    # Keyed on the email as typed, registered or not: limiting only real
    # accounts would answer the question the one-message rule below refuses to.
    email = str(payload.get("email") or "").strip().casefold()
    denied = _slow_down((ratelimit.LOGIN_FAILURES_PER_IP, _client()),
                        (ratelimit.LOGIN_FAILURES_PER_EMAIL, email))
    if denied:
        return denied
    user = db.check_login(payload.get("email", ""), payload.get("password", ""))
    if user is None:
        ratelimit.LOGIN_FAILURES_PER_IP.record(_client())
        ratelimit.LOGIN_FAILURES_PER_EMAIL.record(email)
        # One message for both "no such account" and "wrong password": telling
        # them apart is a way to ask the server which emails are registered.
        return jsonify({"error": "email or password is wrong"}), 401
    tables = db.tables_for_user(user["id"])
    response = jsonify({"ok": True, "tables": [dict(r) for r in tables]})
    return auth.set_cookie(response, auth.USER_COOKIE, user["id"], "user")


@app.post("/api/logout")
@auth.require_json
def api_logout():
    """Forget the DM cookie. Player memberships are per-table cookies and are
    left alone -- signing out of your account should not evict the tablet on
    the table from the game it is in the middle of."""
    return auth.clear_cookie(jsonify({"ok": True}), auth.USER_COOKIE)


@app.get("/api/me")
def api_me():
    """Who am I and what tables can I open. The den's only data source."""
    user = auth.current_user(db)
    if user is None:
        return jsonify({"user": None, "tables": []})
    tables = [
        {"slug": r["slug"], "name": r["name"], "role": r["role"],
         # Owner and DM can be different people -- the owner may be the player
         # who summoned the familiar. The den needs both to know which controls
         # to offer.
         "owner": r["owner_id"] == user["id"]}
        for r in db.tables_for_user(user["id"])
    ]
    return jsonify({"user": {"email": user["email"]}, "tables": tables})


@app.post("/api/tables")
@auth.require_json
def api_create_table():
    """Create a table, its directory, and its first familiar.

    A table with no familiar is a screen with nothing on it, so one is created
    here rather than left as a step the DM has to discover. It is named after
    the table until they rename it.
    """
    user = auth.current_user(db)
    if user is None:
        return jsonify({"error": "sign in first"}), 401
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    slug = (payload.get("slug") or "").strip().lower()
    if not name:
        return jsonify({"error": "a table name is required"}), 400
    try:
        table_id = db.create_table(user["id"], name, slug)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    table = Table(db.table(table_id), DATA_DIR, BASE)
    table.ensure_dirs()
    familiar_name = (payload.get("familiar") or "").strip() or "Familiar"
    # The first familiar belongs to whoever created the table. If that person is
    # a player who bought the service rather than the DM, this is what keeps its
    # persona and voice theirs.
    creator = db.member_for_user(table_id, user["id"])
    table.familiars.create(familiar_name, wake_words=[familiar_name.lower()],
                           owner=creator["id"] if creator else "")
    return jsonify(table.public())


@app.get("/api/t/<slug>/usage")
@require_member(role="dm")
def api_usage():
    """What this table has spent this month.

    `cache_hit_rate` is the number worth watching on a Claude key: if it stays
    at 0.0 across a session of questions, prompt caching is not engaging -- a
    prompt under the ~1024-token minimum simply never caches.
    """
    report = usage_policy.usage_report(db, g.table.id)
    report["history"] = [dict(r) for r in db.usage_history(g.table.id)]
    return jsonify(report)


@app.get("/api/t/<slug>/invite")
@require_member(role="dm")
def api_invite():
    """The link the DM sends their players."""
    return jsonify({
        "url": f"/join/{g.table.invite_token}",
        "members": [
            {"id": m["id"], "name": m["display_name"], "role": m["role"]}
            for m in db.members(g.table.id)
        ],
    })


@app.post("/api/t/<slug>/invite/rotate")
@require_member(role="dm")
@auth.require_json
def api_rotate_invite():
    """Mint a new invite link. The old one stops working immediately -- which
    is the point: it is how a DM un-invites someone who has the link."""
    return jsonify({"url": f"/join/{db.rotate_invite(g.table.id)}"})


# A table has one puppet, usually. The cap is not a licensing decision -- it is
# so that a script hammering the mint endpoint with a stolen DM cookie fills a
# list somebody would notice rather than a disk.
MAX_DEVICES_PER_TABLE = 8


def _device_view(row) -> dict:
    """One device as the DM's screen sees it. Never the token, never its hash."""
    pending = row["token_hash"] is None
    return {
        "id": row["id"],
        "name": row["name"],
        "paired": not pending,
        "pairedAt": row["paired_at"],
        "lastSeenAt": row["last_seen_at"],
        "expiresAt": row["pair_expires"],
        # The code is shown while it is still claimable, and only to the DM who
        # minted it: it is useless after fifteen minutes or one use, and a DM
        # who closed the tab needs to be able to look at it again rather than
        # mint a second one.
        "code": database.format_pair_code(row["pair_code"]) if pending else None,
    }


@app.get("/api/t/<slug>/devices")
@require_member(role="dm")
def api_devices():
    """The puppets paired to this table, and any code waiting to be claimed."""
    db.expire_pair_codes()
    return jsonify({"devices": [_device_view(r) for r in db.devices(g.table.id)]})


@app.post("/api/t/<slug>/devices")
@require_member(role="dm")
@auth.require_json
def api_mint_pair_code():
    """Mint a pairing code for a new puppet. DM only.

    DM only rather than owner: pairing a body is running the table, not paying
    for it, and it is the person sitting at the table with the cat in front of
    them who needs to do it. (Compare the familiar's persona, which belongs to
    whoever summoned it.)
    """
    # Sweep first, then count. The other order is a permanent lockout: eight
    # codes minted during a fumbled SSH session would still be occupying the
    # list an hour later, and the 409 would return before anything got as far
    # as noticing they were dead.
    db.expire_pair_codes()
    if len(db.devices(g.table.id)) >= MAX_DEVICES_PER_TABLE:
        return jsonify({
            "error": f"this table already has {MAX_DEVICES_PER_TABLE} bodies; "
                     "forget one you are not using",
        }), 409
    payload = request.get_json(silent=True) or {}
    minted = db.mint_pair_code(g.table.id, payload.get("name", ""))
    return jsonify({
        "id": minted["id"],
        "name": minted["name"],
        "code": database.format_pair_code(minted["code"]),
        "expiresAt": minted["expires_at"],
        "minutes": database.PAIR_TTL_SECONDS // 60,
    }), 201


@app.delete("/api/t/<slug>/devices/<device_id>")
@require_member(role="dm")
@auth.require_json
def api_forget_device(device_id):
    """Forget a puppet: its token stops working on its next connect.

    This is what "the cat was stolen" or "I am giving it to another table"
    means, and it is per-device on purpose -- revoking one body must not
    disturb the screens or the other bodies at the same table.
    """
    if not db.delete_device(g.table.id, device_id):
        return jsonify({"error": "no such device at this table"}), 404
    return jsonify({"ok": True})


@app.post("/api/pair")
@auth.require_json
def api_pair():
    """Redeem a pairing code for a device token. Called by the puppet itself.

    Unauthenticated, like `/api/join/<token>`, and for the same reason: the
    code *is* the credential. What keeps it safe is not a password in front of
    it but the shape of the code -- ~39 bits of entropy, single-use, and dead
    fifteen minutes after it is minted. Guessing one inside that window is not
    a thing that happens; a leaked one is worth nothing the next morning.

    The response tells the device everything it needs to come back on its own:
    which table it belongs to and the exact bus path to hold. The token is in
    this response and nowhere else, ever again.
    """
    payload = request.get_json(silent=True) or {}
    denied = _slow_down((ratelimit.PAIR_FAILURES_PER_IP, _client()))
    if denied:
        return denied
    claimed = db.claim_pair_code(payload.get("code", ""))
    if claimed is None:
        ratelimit.PAIR_FAILURES_PER_IP.record(_client())
        # One message for "never existed", "already used" and "expired". They
        # are the same fact from the device's side -- try again with a fresh
        # code -- and telling them apart is a way to ask which codes are live.
        return jsonify({
            "error": "that pairing code is not valid (codes are single-use and "
                     "expire after a few minutes) — mint a new one",
        }), 404
    device, token = claimed
    row = db.table(device["table_id"])
    return jsonify({
        "token": token,
        "device": {"id": device["id"], "name": device["name"]},
        "table": {"slug": row["slug"], "name": row["name"]},
        "wsPath": f"/t/{row['slug']}/ws/state",
    })


@app.get("/join/<token>")
def join_page(token):
    """The invite landing page. Serving it through a token check means a stale
    link says so here, rather than after the player has typed their name.

    Shares api_join's limit: this is the same token check, and a guesser would
    simply use whichever of the two was unguarded."""
    if ratelimit.JOIN_FAILURES_PER_IP.retry_after(_client()):
        return app.send_static_file("join-expired.html"), 429
    if db.table_by_invite(token) is None:
        ratelimit.JOIN_FAILURES_PER_IP.record(_client())
        return app.send_static_file("join-expired.html"), 404
    return app.send_static_file("join.html")


@app.post("/api/join/<token>")
@auth.require_json
def api_join(token):
    """Accept an invite: create a member row and hand back its cookie."""
    denied = _slow_down((ratelimit.JOIN_FAILURES_PER_IP, _client()))
    if denied:
        return denied
    row = db.table_by_invite(token)
    if row is None:
        ratelimit.JOIN_FAILURES_PER_IP.record(_client())
        return jsonify({"error": "this invitation is no longer valid"}), 404
    payload = request.get_json(silent=True) or {}
    try:
        member_id = db.add_member(row["id"], payload.get("name", ""), role="player")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    response = jsonify({"slug": row["slug"], "name": row["name"]})
    return auth.set_cookie(response, auth.member_cookie(row["id"]), member_id, "member")


def _may_edit_pc(entry) -> bool:
    """Whether this request may change this character.

    Close to Familiar.may_edit but deliberately *not* the same: the member the
    character is linked to, and the DM as well. A character sheet is written to
    be read out -- the DM needs it to run the game, and none of it is a secret
    from them. A familiar's persona is the opposite case, which is why that one
    shuts the DM out and this one does not. An *unlinked* character stays
    DM-only: a character nobody has claimed cannot be claimed by whoever asks
    first.

    A dangling member_id (the player was removed) simply never matches, so it
    degrades to DM-only with no cleanup code anywhere. That is the same
    accident-of-good-design that familiars already rely on.
    """
    if g.member["role"] == "dm":
        return True
    linked = (entry or {}).get("member_id") or ""
    return bool(linked) and linked == g.member["id"]


@app.get("/api/t/<slug>/party")
@require_member()
def api_party():
    """Who is at this table, in the fiction.

    Readable by every member: the party is not secret, and a player needs to
    see the spelling of their own character's name if the roll tags are ever
    going to match it. `mayEdit` is computed per entry server-side -- the
    client uses it to decide what to render, and the server checks again on
    every write regardless.
    """
    entries = PARTY.listing(g.table)
    for entry in entries:
        entry["mayEdit"] = _may_edit_pc(entry)
    return jsonify({"party": entries, "mayCreate": g.member["role"] == "dm"})


@app.post("/api/t/<slug>/party")
@require_member(role="dm")
@auth.require_json
def api_party_create():
    """Add a character. DM only.

    Creation is where `member_id` is set, which is to say it is where an edit
    permission is granted. A player who could create a character could grant
    themselves that seam, which is the one thing seams exist to prevent. The
    DM is sitting right there at session zero; a compound rule like "a player
    may create one for themselves if they have none" buys very little and is
    exactly where permission bugs live.
    """
    payload = request.get_json(silent=True) or {}
    entry, error = PARTY.create(g.table, g.rt, payload, by=g.member["display_name"])
    if error:
        return jsonify({"error": error}), 400
    return jsonify(entry), 201


@app.put("/api/t/<slug>/party/<pc_id>")
@require_member()
@auth.require_json
def api_party_update(pc_id):
    """Edit a character. The linked player, or the DM.

    `retired` is the ordinary end of a character: they leave the fold-in prompt
    and the default list, and they *keep their dice history*. A character who
    died is not a mistake, and deleting them would delete a part of the
    campaign that actually happened.
    """
    existing = PARTY.get(g.table, pc_id)
    if existing is None:
        return jsonify({"error": "unknown character"}), 404
    if not _may_edit_pc(existing):
        return jsonify({
            "error": f"{existing['name']} is somebody else's character — only "
                     "they or the DM can change it",
        }), 403
    payload = request.get_json(silent=True) or {}
    forbidden = set(payload) & set(PARTY.DM_ONLY_FIELDS)
    if forbidden and g.member["role"] != "dm":
        # Refused, not filtered. Silently dropping the field would tell the
        # caller their change was saved when the one thing they cared about
        # was not.
        return jsonify({
            "error": f"only the DM can set {', '.join(sorted(forbidden))}",
        }), 403
    entry, error = PARTY.update(g.table, g.rt, pc_id, payload)
    if error:
        return jsonify({"error": error}), 400
    if entry is None:
        return jsonify({"error": "unknown character"}), 404
    return jsonify(entry)


@app.delete("/api/t/<slug>/party/<pc_id>")
@require_member(role="dm")
@auth.require_json
def api_party_delete(pc_id):
    """Erase a character. DM only, and rarely what anybody wants -- see
    `retired` on the update route, which is the ordinary way a character
    leaves. Their roll tags survive in the timeline either way; after a delete
    they simply stop matching anybody and show up as unplaced in the stats."""
    if not PARTY.delete(g.table, g.rt, pc_id):
        return jsonify({"error": "unknown character"}), 404
    return jsonify({"ok": True})


# ------------------------------------------------------- who is at the table ---

@app.get("/api/t/<slug>/members")
@require_member(role="dm")
def api_members():
    """The people who can open this table.

    Split out of GET /invite, which used to return the invitation link and the
    member list together. That conflation meant a page showing who is here also
    showed the secret that lets anyone in -- so it could not be put on screen
    while somebody was looking over your shoulder, which is exactly when you
    want to check who is here.
    """
    return jsonify({"members": [
        {"id": m["id"], "name": m["display_name"], "role": m["role"],
         "has_account": bool(m["user_id"])}
        for m in db.members(g.table.id)
    ]})


@app.patch("/api/t/<slug>/members/<member_id>")
@require_member(role="dm")
@auth.require_json
def api_member_rename(member_id):
    """Rename somebody. DM only -- and deliberately not "or yourself".

    A display name is the attribution on a permanent record: it is the `by` on
    every correction that person has ever made (see _append_amendment). A
    self-rename would retroactively relabel all of them, which is a change to
    the history of the table rather than to a profile.
    """
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "a name is required"}), 400
    if db.member_in_table(g.table.id, member_id) is None:
        return jsonify({"error": "unknown member"}), 404
    db.rename_member(g.table.id, member_id, name)
    return jsonify({"ok": True, "name": name})


@app.delete("/api/t/<slug>/members/<member_id>")
@require_member(role="dm")
@auth.require_json
def api_member_remove(member_id):
    """Remove somebody from the table.

    Their corrections stay on the record under the name they made them with --
    an amendment is a statement about what happened at a table, and the table
    keeps its own history. What goes is their way back in.

    Three things dangle afterwards and all three are already correct with no
    cleanup code: their session cookie names a row that no longer exists, so
    the next request is a 401; a familiar they summoned becomes editable by
    nobody, because Familiar.may_edit requires a *matching* owner -- which is
    why the DM keeps may_retire, the one way such a creature can be cleared
    away; and a character linked to them falls back to DM-only via
    _may_edit_pc.
    """
    if not db.remove_member(g.table.id, member_id):
        # remove_member refuses a DM: a table with nobody to run it cannot be
        # administered, and deleting the table is the honest way to end it.
        return jsonify({
            "error": "that member cannot be removed — a table needs its DM",
        }), 400
    return jsonify({"ok": True})


# ------------------------------------------------------------- the familiars ---

@app.post("/api/t/<slug>/familiars")
@require_member(role="dm")
@auth.require_json
def api_familiar_create():
    """Summon a new familiar. DM only, against the intuitive reading.

    In the fiction a *player* summons a familiar, and the first one is already
    assigned to whoever created the table. But binding is checked against the
    familiar being summoned rather than the one currently bound (see
    api_familiar) -- deliberately, so summoning your own creature is your own
    business. That means anyone who can create a familiar can also bind it,
    which is to say take over the table's screen mid-session.

    So the DM creates, and may hand ownership to a player in the same call. The
    fiction survives ("yes, Rowan has an owl") without the screen being open to
    everyone.
    """
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "a familiar needs a name"}), 400
    familiars = g.table.familiars
    if len(familiars.list()) >= MAX_FAMILIARS_PER_TABLE:
        return jsonify({
            "error": f"a table can hold {MAX_FAMILIARS_PER_TABLE} familiars",
        }), 400

    owner = (payload.get("owner") or "").strip()
    if owner and db.member_in_table(g.table.id, owner) is None:
        return jsonify({"error": "that member is not at this table"}), 400

    wanted = familiars_module.slugify_id(name)
    if familiars.get(wanted) is not None:
        return jsonify({"error": f"there is already a familiar called {name}"}), 409
    try:
        familiar = familiars.create(name, wake_words=[name.lower()], owner=owner)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(familiar.public()), 201


@app.patch("/api/t/<slug>/familiars/<familiar_id>")
@require_member()
@auth.require_json
def api_familiar_update(familiar_id):
    """Change a familiar: its name, its wake words, its shell, its sounds.

    Checked against the *named* familiar rather than whichever one is bound --
    the same rule binding uses, and the fix for a bug the old /persona route
    had, where a player who had summoned Pip was refused because Grix happened
    to be on screen.

    `owner` is DM-only within an otherwise summoner-editable route: it is the
    field that decides who may edit, so a player reassigning it would be
    granting a permission. Refused rather than dropped.

    It is also the one field a DM may set on somebody else's familiar, which is
    why the gate below is may_retire when `owner` is all that is being changed.
    Who a creature belongs to is a fact about who is at the table -- the same
    kind of fact as dismissal, and the DM's business for the same reason --
    while what that creature *is* stays private to its summoner. Without this,
    a familiar whose player left the group could only ever be deleted, taking
    its persona with it, when handing it to somebody else was what was wanted.
    """
    familiar = g.table.familiars.get(familiar_id)
    if familiar is None:
        return jsonify({"error": "no such familiar"}), 404
    payload = request.get_json(silent=True) or {}
    reassigning_only = set(payload) == {"owner"}
    denied = (_may_retire_familiar(familiar) if reassigning_only
              else _may_edit_familiar(familiar))
    if denied:
        return denied

    fields = {}
    if "owner" in payload:
        if g.member["role"] != "dm":
            return jsonify({"error": "only the DM can reassign a familiar"}), 403
        owner = (payload["owner"] or "").strip()
        if owner and db.member_in_table(g.table.id, owner) is None:
            return jsonify({"error": "that member is not at this table"}), 400
        fields["owner"] = owner
    if "name" in payload:
        name = (payload["name"] or "").strip()
        if not name:
            return jsonify({"error": "a familiar needs a name"}), 400
        fields["name"] = name
    if "wake_words" in payload:
        words = payload["wake_words"]
        if isinstance(words, str):
            words = [w.strip() for w in words.split(",")]
        if not isinstance(words, list) or any(not isinstance(w, str) for w in words):
            return jsonify({"error": "wake words must be a list of words"}), 400
        words = [w.strip().lower() for w in words if w and w.strip()]
        if not words:
            # An empty list would leave a familiar that cannot be woken by
            # name, and nothing on the screen would explain why.
            return jsonify({"error": "a familiar needs at least one wake word"}), 400
        fields["wake_words"] = words
    for key, allowed in (("shell", _shell_ids()), ("sounds", _sound_ids())):
        if key in payload:
            value = (payload[key] or "").strip()
            if value not in allowed:
                return jsonify({"error": f"no such {key}: {value!r}"}), 400
            fields[key] = value

    if not fields:
        return jsonify({"error": "nothing to change"}), 400
    return jsonify(g.table.familiars.update(familiar_id, **fields).public())


@app.delete("/api/t/<slug>/familiars/<familiar_id>")
@require_member()
@auth.require_json
def api_familiar_delete(familiar_id):
    """Dismiss a familiar for good, with its persona and its bond.

    Refuses the last one: a table with no familiar is a screen with nothing on
    it and no route back. Deleting the bound one is allowed -- active() falls
    back to the first remaining familiar, so the table always has somebody.
    """
    familiar = g.table.familiars.get(familiar_id)
    if familiar is None:
        return jsonify({"error": "no such familiar"}), 404
    # The one route that takes _may_retire_familiar rather than
    # _may_edit_familiar: a DM may clear away an abandoned familiar without
    # ever being able to read what it was.
    denied = _may_retire_familiar(familiar)
    if denied:
        return denied
    if not g.table.familiars.delete(familiar_id):
        return jsonify({
            "error": "this is the table's only familiar — summon another first",
        }), 400
    g.rt.history.clear()
    return jsonify({"ok": True})


def _asset_ids(kind: str) -> set:
    """The shipped shell and sound-pack libraries.

    Read from Flask's static folder, NOT from BASE. The two are usually the
    same directory, but BASE is the *application data* root and moves with the
    deployment (and in tests points at a tmp dir), while shells and sound packs
    are assets shipped with the code. A shell is a drawing, not somebody's
    campaign -- see the split described in tenancy.py.

    Read from disk rather than listed in code, so dropping a directory in is
    all it takes to offer a new one.
    """
    try:
        return {p.name for p in (Path(app.static_folder) / kind).iterdir() if p.is_dir()}
    except OSError:
        return set()


def _shell_ids() -> set:
    return _asset_ids("shells")


def _sound_ids() -> set:
    return _asset_ids("sounds")


@app.get("/api/t/<slug>/shells")
@require_member()
def api_shells():
    """What a familiar can look like and sound like. Both libraries are shipped
    with the app rather than owned by a table -- a shell is a drawing, not
    somebody's campaign."""
    return jsonify({"shells": sorted(_shell_ids()), "sounds": sorted(_sound_ids())})


def _text_revision(text: str) -> str:
    """A fingerprint of one document, for the same reason _campaign_revision
    exists: two tabs open on the same persona is one person, not two."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _campaign_revision(table) -> str:
    """A fingerprint of both campaign files, for optimistic concurrency.

    Two DMs on two phones is not exotic -- it is one person on a laptop and the
    same person on the tablet they left open at last week's session. Without
    this, the second save silently discards the first, and neither of them ever
    finds out. sha256 of the bytes rather than an mtime: mtimes have one-second
    resolution on some filesystems and go backwards when a file is restored.

    Covers dm-only.md as well as campaign.md, because one form saves both.
    """
    digest = hashlib.sha256()
    for path in (table.dir / "campaign.md", table.dm_only_file, table.dm_passphrase_file):
        digest.update(path.read_bytes() if path.exists() else b"")
        digest.update(b"\0")
    return digest.hexdigest()[:16]


@app.get("/api/t/<slug>/campaign")
@require_member()
def api_get_campaign():
    """The campaign notes, as fields.

    Readable by every member, writable by the DM. That is a deliberate loosening
    of what came before, where the whole document was withheld from anyone but
    the DM because it carried a "## DM only" section inline -- withholding
    everything was the only safe move when secrets and house rules lived in one
    file. They no longer do. House rules and where the party is are things
    players should be able to read; the familiar recites them aloud anyway.

    `dmOnly` is present only for the DM, and omitted rather than blanked for
    anyone else: a key that is sometimes a secret and sometimes an empty string
    is a key somebody will one day forget to check.
    """
    fields = _campaign_fields(g.table)
    body = {
        "fields": {key: fields[key] for key in campaign_notes.FIELD_KEYS},
        "labels": dict(campaign_notes.FIELDS),
        # What each section is for. Served rather than written into the form so
        # the hint and the field it describes cannot drift apart.
        "hints": dict(campaign_notes.HINTS),
        "mayEdit": g.member["role"] == "dm",
        "revision": _campaign_revision(g.table),
    }
    if g.member["role"] == "dm":
        table = g.table
        stored = (table.dm_only_file.read_text(encoding="utf-8")
                  if table.dm_only_file.exists() else "")
        # A stray "## DM only" still sitting in campaign.md is shown in the box
        # it belongs in, so that saving the form is what finally moves it.
        body["dmOnly"] = "\n\n".join(
            part for part in (stored.strip(), fields["dm_only_stray"]) if part)
        body["passPhrase"] = dm_passphrase(table)
    return jsonify(body)


@app.put("/api/t/<slug>/campaign")
@require_member(role="dm")
@auth.require_json
def api_put_campaign():
    """Save the campaign notes. DM only.

    The campaign belongs to the table, not to whoever summoned the familiar --
    the same seam the memory splits along, and the reason this is role="dm"
    rather than _may_edit_familiar.

    Body: {fields: {...}, dmOnly?: str, revision?: str}. A `revision` that no
    longer matches gets a 409 carrying the server's current state, so the
    client can show both rather than picking a winner on the user's behalf.
    Omitting it is allowed and means "I accept whatever is there" -- which is
    what a script wants and what a form must never send.
    """
    payload = request.get_json(silent=True) or {}
    table = g.table
    table.ensure_dirs()

    with g.rt.campaign_lock:
        if payload.get("revision") and payload["revision"] != _campaign_revision(table):
            current = _campaign_fields(table)
            return jsonify({
                "error": "these notes were changed by somebody else since you "
                         "opened them — your text is below, theirs is on the server",
                "fields": {key: current[key] for key in campaign_notes.FIELD_KEYS},
                "revision": _campaign_revision(table),
            }), 409

        submitted = payload.get("fields")
        if not isinstance(submitted, dict):
            return jsonify({"error": "fields must be an object"}), 400
        unknown = set(submitted) - set(campaign_notes.FIELD_KEYS)
        if unknown:
            return jsonify({"error": f"unknown field(s): {', '.join(sorted(unknown))}"}), 400

        existing = _campaign_fields(table)
        fields = {key: submitted.get(key, existing[key]) for key in campaign_notes.FIELD_KEYS}
        for key, value in fields.items():
            if not isinstance(value, str):
                return jsonify({"error": f"{key} must be text"}), 400

        phrase = payload.get("passPhrase")
        if phrase is not None:
            if not isinstance(phrase, str):
                return jsonify({"error": "passPhrase must be text"}), 400
            phrase = " ".join(phrase.split())
            if phrase and len(" ".join(_speech_words(phrase))) < PASSPHRASE_MIN_CHARS:
                return jsonify({"error": "make the pass-phrase at least "
                                         f"{PASSPHRASE_MIN_CHARS} letters long"}), 400

        # Secrets first, notes second. render() drops dm_only_stray, so if the
        # order were reversed a stray section would be erased from campaign.md
        # before anything had written it down.
        _save_dm_only(table, payload.get("dmOnly"), existing["dm_only_stray"])
        if phrase is not None:
            table.dm_passphrase_file.write_text(phrase + "\n" if phrase else "",
                                                encoding="utf-8")
        (table.dir / "campaign.md").write_text(campaign_notes.render(fields), encoding="utf-8")
        saved = _campaign_fields(table)

    return jsonify({
        "ok": True,
        # The re-parsed fields, not the submitted ones: the box should show
        # what the file now says, canonical headings and all.
        "fields": {key: saved[key] for key in campaign_notes.FIELD_KEYS},
        "revision": _campaign_revision(table),
    })


def _save_dm_only(table, submitted, stray: str) -> None:
    """Write dm-only.md, absorbing anything still stranded in campaign.md.

    `stray` is a "## DM only" section a DM typed into campaign.md out of habit.
    It has to be captured here because the very next line of the caller renders
    campaign.md without it. Skipped when it is already present, so saving the
    form twice does not leave two copies.
    """
    if submitted is None and not stray:
        return
    current = (table.dm_only_file.read_text(encoding="utf-8")
               if table.dm_only_file.exists() else "")
    text = submitted if isinstance(submitted, str) else current
    if stray and stray.strip() not in text:
        text = "\n\n".join(part for part in (text.strip(), stray.strip()) if part)
    table.dm_only_file.write_text(text.strip() + "\n" if text.strip() else "",
                                  encoding="utf-8")


def _named_familiar(familiar_id: str):
    """(familiar, refusal). Resolved by id, and checked against *that* familiar
    rather than whichever one happens to be bound.

    The old /persona pair got this wrong: it resolved active() and then checked
    edit rights on it, so a player who had summoned Pip was refused with
    "Grix was summoned by someone else" and could never reach their own
    creature while Grix was on screen. api_familiar already had it right, and
    its comment says why -- summoning your own familiar is your business, and
    should not depend on who is present at the time.
    """
    familiar = g.table.familiars.get(familiar_id)
    if familiar is None:
        return None, (jsonify({"error": "no such familiar"}), 404)
    if not familiar.may_edit(g.member):
        return None, (jsonify({
            "error": f"{familiar.name} was summoned by someone else — only they "
                     "can change it",
        }), 403)
    return familiar, None


@app.get("/api/t/<slug>/familiars/<familiar_id>/persona")
@require_member()
def api_get_familiar_persona(familiar_id):
    """Who this familiar is. Editing it in the browser is the feature this
    endpoint exists for -- before it, changing a persona meant an SSH session.

    `revision` works as it does for the campaign notes: echo it back on the PUT
    and a save that would overwrite somebody else's gets a 409 instead.
    """
    familiar, refusal = _named_familiar(familiar_id)
    if refusal:
        return refusal
    text = g.table.familiars.persona_text(familiar)
    return jsonify({
        "persona": text,
        "familiar": familiar.public(),
        "revision": _text_revision(text),
    })


@app.put("/api/t/<slug>/familiars/<familiar_id>/persona")
@require_member()
@auth.require_json
def api_put_familiar_persona(familiar_id):
    familiar, refusal = _named_familiar(familiar_id)
    if refusal:
        return refusal
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("persona"), str):
        return jsonify({"error": "persona must be text"}), 400

    g.table.ensure_dirs()
    current = g.table.familiars.persona_text(familiar)
    if payload.get("revision") and payload["revision"] != _text_revision(current):
        return jsonify({
            "error": "this persona was changed by somebody else since you "
                     "opened it — your text is below, theirs is on the server",
            "persona": current,
            "revision": _text_revision(current),
        }), 409

    familiar.persona_path.parent.mkdir(parents=True, exist_ok=True)
    familiar.persona_path.write_text(payload["persona"], encoding="utf-8")
    return jsonify({"ok": True, "revision": _text_revision(payload["persona"])})


if __name__ == "__main__":
    use_https = "--https" in sys.argv
    host = os.environ.get("HOST", "0.0.0.0")  # 127.0.0.1 when behind Caddy
    port = int(os.environ.get("PORT", "8321"))
    # threaded: each /ws/state body holds a long-lived connection in its own
    # thread, so ordinary /api requests must be served concurrently.
    kwargs = {"host": host, "port": port, "threaded": True}
    if use_https:
        kwargs["ssl_context"] = "adhoc"  # needs: pip install pyopenssl
    scheme = "https" if use_https else "http"
    print(f"The familiars are waking up on {scheme}://{host}:{port} ...")
    for row in db.all_tables():
        print(f"  {row['name']}  ->  {scheme}://{host}:{port}/t/{row['slug']}/")
    app.run(**kwargs)
