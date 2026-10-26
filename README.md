# Familiar — a creature that sits in on your D&D game

<!-- TODO: docs/familiar.gif — the cat waking and answering a question. -->

Put a tablet or laptop at the end of the table and a familiar lives on it: a
cat (or an owl, or any of the fifteen *find familiar* forms) that answers to its
name, knows your campaign, and replies **out loud, in character** to whatever
anyone asks — "Whiskers, how does grappling work?". If your table lets it, it
also listens to the evening and keeps the record nobody was ever going to keep:
a **timeline** of what happened, a **cast** of everyone you met, the **places**
you've been, and an in-game **calendar** counted from your long rests — all
searchable, and all correctable from any player's phone. There's an optional
**physical puppet**, too: a Pi and six servos that move while it speaks.

It's free to run: the brain is Google Gemini's free tier and the voice is
Piper, running on your own machine. MIT licensed.

There are three ways to run it. Each builds on the one before.

## Tier 1 — on your laptop

You need Python 3.10+ and Chrome (or Edge). Linux or macOS; on Windows, use
[WSL](https://learn.microsoft.com/windows/wsl/install).

```sh
git clone https://github.com/<you>/familiar.git && cd familiar
./setup.sh                     # venv, dependencies, a voice, and .env
.venv/bin/python server.py
```

`setup.sh` asks for a Gemini key — get one free at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey).

Open **http://localhost:8321**, make an account (the first one owns the
install), and create a table — you get a familiar with it. Tap the creature to
wake it, allow the microphone, then say its name and a question.

Write your campaign notes in the den (the party, where you are, people you've
met, secrets marked *DM only*), and flip on **Record table talk** in the ⚙
panel if your table agrees to it. The timeline, cast and places fill in as you
play.

**Players at the same table** can follow along on their phones: send them the
invite link from the den, with your laptop's address in place of `localhost` —
`http://<laptop-ip>:8321/...`. They can read and correct the timeline during
the session. (Their phones can't be the familiar's *ears* over plain http —
browsers only allow the microphone on HTTPS or localhost — but they don't need
to be.)

## Tier 2 — add the puppet

A Raspberry Pi Zero 2 W, a PCA9685 servo board, six SG90 servos and a cat to put
them in — about £45–70. The full build guide is [`pi/README.md`](pi/README.md).

The puppet joins the same table over your wifi. In the browser go to *Running
the table → Bodies → Pair a puppet*, then on the Pi:

```sh
python body_client.py --pair WXYZ-2345 --host http://<laptop-ip>:8321
```

It pairs once and remembers. Without `--real` it's a mock that prints every
servo move, so you can try the whole chain before you've bought a servo.

## Tier 3 — an always-on server

Run it on a VPS, a home server or a Pi 4 with a domain name, and everyone
reaches it over HTTPS: players open their invite links on their phones and
review the timeline between sessions, and an iPad at the table can be the
familiar's body (and ears).

```sh
git clone https://github.com/<you>/familiar.git && cd familiar
cp .env.example .env    # SECRET_KEY, GEMINI_API_KEY, SIGNUP_CODE, DOMAIN
docker compose --profile https up -d
```

Caddy fetches the certificate automatically. Set `SIGNUP_CODE` so strangers
who find the address can't make accounts — players never need one, only DMs.
There's a systemd route too, plus nightly backups to S3 or another disk: see
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

**Moving up from tier 1** is copying your laptop's `data/` folder to the server
— a table is just a folder.

## Make your own familiar

A familiar is a name, some wake words, a shell (what it looks like) and a
persona (who it is). Make one in the browser, then write its persona: what it
is, how it talks, what it loves and hates. Two finished ones to borrow from are
in [`examples/familiars/`](examples/familiars/): **Whiskers**, a vain grey
tabby, and **Pellinore**, a pompous old barn owl.

- **Several per table.** Swap between them from the ⚙ panel; the campaign,
  timeline and cast stay put, and each familiar keeps its own relationship with
  the party.
- **A familiar belongs to whoever summoned it.** A player can make their own
  and edit its persona; the campaign notes stay the DM's.
- **Wake words**: add the ways the speech engine mishears its name. The
  browser console (F12) shows what it actually heard.
- **Shells** live in `static/shells/<id>/` — one SVG and one CSS file animated
  off five states. **Sounds** go in `static/sounds/<id>/`.

## Choose a brain

`LLM_PROVIDER` in `.env` is a chain, tried left to right:

| | Cost | Notes |
|---|---|---|
| `gemini` (default) | free tier | fast with thinking off (the default) |
| `claude` | paid, per token | `pip install anthropic`, set `ANTHROPIC_API_KEY`. The den shows each month's cost |
| `local` | free | Ollama, LM Studio or llama.cpp on a PC in your house: `LOCAL_LLM_URL` |

`local,gemini` asks the PC first and falls back to Gemini when it's off.
Voices are a chain too (`TTS_PROVIDER`): Piper by default, or pocket-tts,
Gemini or ElevenLabs; if every one fails the browser speaks for itself.

## Microphone & privacy

A microphone on a table full of friends deserves a straight account:

- **The mic is off until somebody wakes the familiar**, and table-talk
  recording is a separate switch, off by default, per device, per session. A
  "● recording table talk" indicator is visible the whole time it's on.
- **The server never receives audio.** Speech is turned into text in the
  browser. But the browser's speech engine is usually a cloud service:
  **Chrome sends audio to Google, and Safari (and every browser on an iPhone or
  iPad) sends it to Apple.** If that matters to your table, leave table-talk
  recording off.
- **Text goes to your chosen brain** — Google for Gemini, Anthropic for Claude,
  or nowhere if it's a model on your own network. Voices are made on your
  machine.
- **Everything else stays in `data/`**, as plain text and JSON: a table's whole
  history is a few hundred kilobytes. The DM can export it as a zip or delete
  it outright from the den.
- **Players consent for themselves.** The invite page explains recording before
  anyone joins and needs an explicit tick. If anyone says no, leave the switch
  off — everything else works the same.

`static/privacy.html` is the longer version, linked from the invite page.

## More

- [`docs/HOW-IT-WORKS.md`](docs/HOW-IT-WORKS.md) — session memory, the in-game
  calendar, search, corrections, backups, and the API
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — running a server
- [`pi/README.md`](pi/README.md) — building the puppet
- [`CREDITS.md`](CREDITS.md) — assets, voices and licences

Run the tests with `.venv/bin/pip install -r requirements-dev.txt &&
.venv/bin/pytest`.
