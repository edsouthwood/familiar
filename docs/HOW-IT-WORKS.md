# How it works

The deep end: what the familiar does with what it hears, where everything is
kept, and the API underneath. For getting it running, start with the
[README](../README.md); for running a server, [OPERATIONS.md](OPERATIONS.md).

## Tables, familiars and people

A **table** is one campaign: its notes, its memory, its cast list, its session
history, and the familiars that sit in on it. Everything a table owns lives in
one directory, `data/tables/<id>/`, so backing one up is copying a folder.

Whoever sets the table up signs in with an account. **Players** don't need one —
send them the invitation link from the den, they type a name, and they're in.
That name is what appears beside any correction they make to the session record.

**A familiar belongs to whoever summoned it, which need not be the DM.** In the
fiction a familiar is a player's, so permissions follow the creature rather
than the chair:

| Yours if you summoned it | The DM's |
|---|---|
| The familiar's persona and bond | The campaign notes |
| Its voice | The cast list and session timeline |
| Whether it's the one bound | Invitations, and the usage readout |

The DM can also edit any familiar at their table — somebody has to be able to
retire one whose summoner has left the group. A familiar with no recorded
summoner stays DM-only.

The **den** (`/`) is where your tables are listed, made, and administered: the
invitation link, and the two documents that make the familiar what it is.

## Where a familiar lives

A familiar is a directory. You can have several and swap between them from the
gear panel — the campaign stays put, the creature changes.

| Where | What it is |
|-------|------------|
| `familiars/<id>/familiar.json` | Name, wake words, language, voice, shell and sound pack. |
| `familiars/<id>/persona.md` | That familiar's personality and speaking rules (starts from `persona.example.md`; see `examples/familiars/`). |
| `familiars/<id>/bond.md` | Auto-maintained: its standing with the party. Don't hand-edit. |
| `familiars/.active` | Which familiar is currently bound. |
| `campaign.md` | Campaign notes — NPCs, recaps, house rules, DM secrets. Shared by every familiar; reloaded every question, no restart. Editable from the den. |
| `game-context.md` | Auto-generated running memory of ambient table talk (see below) — shared, and don't hand-edit, it gets overwritten. |
| `static/shells/<id>/` | An appearance: `shell.svg` + `shell.css`. Ships with all fifteen *find familiar* forms. |
| `static/sounds/<id>/` | Creature noises (`.mp3`/`.wav`/`.ogg`) for a sound pack. |
| `voices/` | Clonable/catalogue TTS voices, shared by all familiars; a familiar names one. |

**Persona and bond belong to the familiar; campaign, memory, cast and timeline
belong to the table.** That's what makes swapping coherent: a new familiar
inherits everything that happened, and starts its own relationship with the
party.

The rows above `static/` are per-table: they live under `data/tables/<id>/`. The
`static/` rows are shipped assets shared by every table — a shell is a drawing,
not somebody's campaign. `voices/` is shared for the same reason.

## Writing a shell

A shell is `static/shells/<id>/shell.svg` + `shell.css`. The whole contract is:

- the SVG root is `<svg id="familiar" viewBox="...">`
- the CSS animates it off `body[data-state="asleep|idle|listening|thinking|speaking"]`

Nothing else is assumed — the owl shares no element with the cat beyond that
root, and expresses "thinking" as a head tilt where the cat wags its tail.

## Tuning how quickly it answers

The listening timings live in the `CONFIG` block at the top of
`static/familiar.js`. The one most worth knowing about is **`finalGraceMs`**
(default `900`): once the speech engine marks a phrase as *final* it has already
decided you stopped talking, so the familiar waits only this long before
answering, instead of the full `silenceMs` (`1600`) pause it uses while you're
still mid-phrase. Lower it for snappier replies; if it starts cutting people off
who pause mid-thought, raise it back toward `1200`–`1400`.

## What a table costs

Every LLM and speech call is metered against the table that made it, and the
totals show up in the den: requests, tokens in and out, characters spoken, and
an estimated cost when the brain is a paid one. On the default Gemini free tier
the cost line is left blank rather than guessed at.

Ambient fold-ins have a brake — `FOLD_INS_PER_HOUR`, 12 by default — because
they are the only LLM call a player can trigger without asking anything. A
recorder left running in somebody's bag should not run up your bill. Over the
cap the chunk is dropped rather than refused: the browser doesn't wait for
`/api/ambient`, so an error there would be invisible anyway.

**On a Claude key, the line worth watching is the cache hit rate.** Prompt
caching engages *silently* — a prompt under the minimum cacheable length simply
never caches, with no error anywhere. The system prompt is deliberately split so
the breakpoint falls between the parts that sit still (persona, campaign) and
the parts every fold-in rewrites (bond, running memory). If the den shows
**0%**, the stable half is too short to cache: write more campaign notes, or
budget the uncached rate.

## Switching brains

`LLM_PROVIDER` is a comma-separated chain, tried in order:

```sh
# LLM on a PC in your house first, Gemini when that PC is off:
export LLM_PROVIDER=local,gemini
export LOCAL_LLM_URL=http://192.168.1.50:11434   # Ollama (or LM Studio :1234)
export LOCAL_LLM_MODEL=llama3.1:8b
```

`local` speaks the OpenAI-compatible `/v1/chat/completions` API, so Ollama,
LM Studio, and llama.cpp all work — just point the URL at whichever. (On the
Ollama PC set `OLLAMA_HOST=0.0.0.0` so other machines can reach it; in LM
Studio tick "Serve on Local Network".)

```sh
# Or Claude:
.venv/bin/pip install anthropic
export LLM_PROVIDER=claude
export ANTHROPIC_API_KEY="sk-ant-..."
```

Everything else stays the same — `brain.py` caches the persona + campaign
notes between questions (Claude) so each answer only pays for the new turn.

```sh
# Or pay only where it's felt: spoken answers via Claude (fast), while
# ambient listening and recaps stay on the free chain.
export LLM_PROVIDER=gemini
export LLM_PROVIDER_REPLY=claude,gemini
```

## The voice

`TTS_PROVIDER` is also a chain: `piper` (free, offline, synthesised on the
server — the default), `gemini` (free tier is only ~10 requests/day),
`elevenlabs`, and `pocket` (Kyutai's pocket-tts; `pip install -r requirements-pocket.txt`, ~700 MB of PyTorch). If every provider fails, the browser speaks with its own voice.
Piper voices live in `voices/`; fetch more with
`python -m piper.download_voices --data-dir voices <name>` and pick with
`PIPER_VOICE` (a catalogue name or a path to any community `.onnx`).

## Session memory: ambient capture

By default the familiar only "hears" what happens after its wake word. Turn on
**"Record table talk for session notes"** in the ⚙ settings panel and it also
picks up ambient chat — what the party's actually doing — without being asked
directly. It's opt-in and off by default: nothing ambient leaves the browser
until you switch it on, and a **"● recording table talk"** indicator stays
visible near the status line the whole time it's on, so the toggle's effect
is never hidden inside a settings panel.

How it works, end to end:

1. While idle and the toggle is on, speech that isn't the wake word is
   batched client-side (`static/familiar.js`) instead of being discarded.
2. Once ~500 words accumulate, the browser posts the batch to
   `POST /api/ambient` and keeps listening — this is fire-and-forget, it
   never blocks or interrupts play.
3. The server folds the new chunk into `game-context.md`: an LLM call merges
   it with whatever's already there, producing an *updated* memory rather
   than appending to it, capped at roughly 400 words so it stays a small,
   fixed cost on every question rather than growing across a whole campaign.
4. The memory is split into three sections in priority order, so it knows
   what to protect when it has to trim for length:
   - **Party & relationships** — the familiar's standing with each party member
     and the dynamics between them; changes rarely, carried forward as-is
     unless the transcript reveals something new.
   - **Ongoing threads** — unresolved quests/promises/dangers.
   - **Recent developments** — the freshest events; the first thing trimmed.
5. The same call also extracts three *uncapped, permanent* records, outside
   that memory: new **timeline events** (→ `sessions/<date>-timeline.log`,
   tagged when a bullet clearly describes a dice roll, a rest or the weather),
   new or changed **NPCs** (→ `cast.json`), and named **places** the party
   visited or learned about (→ `locations.json`). See
   [Reviewing and correcting the history](#reviewing-and-correcting-the-history)
   below.
6. `game-context.md` is loaded into the system prompt alongside `persona.md`
   and `campaign.md` on every question, so the familiar carries this forward into
   later answers — and later sessions — without anyone maintaining it by hand.
   Content marked `## DM only` in `campaign.md` is never fed into this
   process, so secrets can't leak into either auto-generated file.

## What day is it? — the in-game clock

Nobody tracks the date at the table, so the familiar derives it from the one
thing a party reliably announces out loud: **"we take a long rest."**

A timeline bullet can carry small structured tags alongside its text, written
by the same fold-in call that writes the bullet:

| Tag | Written when | Effect |
|---|---|---|
| `{roll: character=Tam, outcome=crit}` | a bullet describes a dice roll | feeds **Stats** |
| `{rest: long}` / `{rest: short}` | the party actually *took* a rest | a long rest ends the in-game day |
| `{weather: heavy rain}` | the weather was mentioned | shown beside the day it belongs to |

The day number is **derived, never stored** — recomputed from the
amendment-resolved timeline every time anyone asks, exactly like the dice
tallies in Stats. That is the point: the familiar will sometimes hear a party
*wondering* about a long rest and record one they never took, and the fix is an
ordinary amendment on the timeline page with no separate "recalculate the
calendar" step to forget. Clear the rest and the clock moves back on its own.

It is a plain counter — day 1, day 2 — not a Harptos date. A real calendar
would be a later *reading* of the same numbers and needs nothing re-recorded.

The current day (and recent weather) is fed into the familiar's system prompt,
so asking "what day is it?" or "how long have we been on the road?" gets an
answer. It sits in the volatile half of the prompt, after the cache breakpoint,
because it moves every time the party sleeps. Until something has actually been
recorded, it says nothing at all — an invented day would be worse than none.

The timeline page draws a divider between days, and
`GET /api/t/<slug>/calendar` returns the whole count.

## Places

`locations.json` is the map's answer to `cast.json`. The same fold-in call that
notices a new NPC notices a named place — "the Sunken Chapel", "Harrow's Ford"
— and writes one bullet per place, description and all. Somewhere the party
would name again earns an entry; passing scenery does not.

Places are a **noun**, which is why they are a list and not a timeline tag.
Compare rests and weather above: those are *moments*, so they annotate an event.
A place accrues description over time and gets returned to, exactly like a
person — so it is stored, amended and displayed exactly like a person. Server
side it is literally the same class (`Roster`) pointed at a different file, and
the Places page is the Cast page's renderer pointed at a different endpoint.

**Known sharp edge**: an entry is keyed by a slug of its name, so if the model
calls somewhere "the Minecart Track" this session and "the Collapsed Tunnel"
next, you get two entries rather than one updated. NPC names are stable enough
in practice that this rarely bites the cast; place names are looser, so it will
bite here more often. Merging is a manual amend for now, and no automatic
fuzzy-matching is planned — silently merging two genuinely different caves
would be worse than showing two cards.

## Search

One box over everything the table has recorded — timeline events and the
corrections layered on them, the cast, the places, and every question anyone
has asked the familiar. `GET /api/t/<slug>/search?q=` behind
`/t/<slug>/search.html`, with filter chips per kind and a link from each
timeline hit straight to that event in the viewer.

Two things about it are worth knowing.

**The index is derived, never authoritative.** Every row in the `search` table
is a copy of something already written under `data/tables/<id>/`. That is what
lets every write into it be best-effort: a failed index write costs a search
miss, not an event, and `tools/reindex_search.py` rebuilds the whole thing from
the files. Run it after restoring or copying in a table, or any time results
look stale. It makes no LLM
calls and costs nothing:

```sh
python tools/reindex_search.py                 # every table
python tools/reindex_search.py --table <slug>   # just one
python tools/reindex_search.py --dry-run       # count, change nothing
```

**Whatever you type is treated as words, not as a query language.** SQLite's
FTS5 has a real syntax — `AND`, `OR`, `NOT`, `NEAR`, column filters,
parentheses — and feeding a search box straight into it means a stray quote or
a lone hyphen raises an error at the player. So every whitespace-separated run
becomes a quoted phrase, and no input can be a syntax error. The last word gets
a prefix match, so "chap" finds "chapel" while you are still typing. The cost is
that `chapel OR crypt` searches for those three words; that is the right trade
for a box nobody will read the manual for.

Search results are scoped to one table in SQL rather than filtered afterwards.
The index is the one place in the schema where every tenant's prose sits in a
single table, so the `WHERE table_id = ?` is the tenancy boundary, and it lives
where a route cannot forget it.

## Reviewing and correcting the history

Nobody amends anything at the table — the LLM's best guess is what gets
captured live, and table friction stays at zero. Afterwards, any player can
open **Session timeline**, **Cast & stats**, **Places** or **Search** (linked
from the ⚙ settings panel, the den, and from each other) from their own phone
and fix mistakes:

- Click **amend** on a timeline event, an NPC card or a place card to add a
  correction.
  Amending never rewrites what was actually captured — the original stays
  visible (expand "amended…" to see it), and each correction is recorded with
  who made it and when. If two players correct the same event differently,
  both corrections just sit there for the group to sort out; nothing is ever
  silently overwritten.
- Amending a roll's outcome (or adding one to an event that didn't have one)
  updates **Stats** immediately — the tally is computed fresh from the
  current, amendment-aware text every time you load the page, so there's no
  separate step to "fix" the numbers.
- The same applies to rests and the weather. Setting `—` on the rest
  dropdown **clears** a rest the familiar imagined, and the in-game day count
  shifts back immediately. Adding one the familiar missed advances it.
- Corrections to the free-text narrative (`game-context.md`, NPC
  descriptions) don't retroactively rewrite themselves the same way, since
  they're LLM-synthesized prose, not a counter. After a review pass, hit
  **Apply corrections to memory** on the timeline page
  (`POST /api/sessions/<date>/reprocess`) to regenerate the memory and cast
  from that date forward using the corrected event text. This replays through
  the LLM, so it isn't instant — don't run it mid-session, only after.
- **Who amended it** is whoever is signed in — the DM by account, a player by
  the name they gave when they followed the invitation link. The browser does
  not get to choose the name that goes on a permanent record.

## Getting your data out, and getting rid of it

- **Export** — `GET /api/t/<slug>/export`, owner or DM, returns a zip of the
  whole table directory plus a README naming each file. Everything in it is
  plain text or JSON and readable without this app. Built in memory: a
  table's entire history is a few hundred kilobytes of prose.
- **Delete a table** — `POST /api/t/<slug>/delete`, **owner only**, with the
  table's slug echoed back in the body. Irreversible. A table lives in three
  stores — rows in `app.db`, files under `data/tables/<id>/`, and the FTS
  index — and only the first two are joined by a foreign key, so
  `delete_table_completely()` clears all three and is the only thing that
  should.
- **Leave a table** — `POST /api/t/<slug>/leave`, for a player giving up their
  own membership. Their corrections stay on the record under the name they made
  them with: an amendment is a statement about what happened at a table, and
  the table keeps its own history. What goes is the way back in. The owner
  can't leave (there'd be nobody to answer for the table) and neither can a DM
  (there'd be nobody to run it).
- **Delete an account** — `POST /api/account/delete`, password required.
  Takes every table you *own*; leaves tables you merely play at alone.

The first three are in the den, in the ordinary panel rather than behind a red
"danger zone" — taking your own data back is a normal thing to want, and a
danger zone is where people learn not to look. Account deletion has no button
yet; it is the API call above, or the operator does it.

## Consent and privacy pages

- **`join.html`** — the invitation page — explains recording *before* the name
  field, names the switch exactly as it appears on the device, and requires an
  explicit tick. A DM cannot consent to a microphone on behalf of five other
  people, and the invitation is the one door all of them pass through.
- **`privacy.html`** is the long version, written from what the code does. Two
  claims it deliberately does *not* make: that audio never leaves the room (the
  browser's own speech engine sends it to Google on Chrome and Apple on iOS —
  the server never sees it, but that is a different sentence), and that voice
  samples are taken from anyone.

## Backups

> Running a server day to day — standing it up, the nightly backup, what to
> check when something is wrong — is in **[`OPERATIONS.md`](OPERATIONS.md)**.
> This section is the why; that is the runbook.

The one thing that protects a table from the disk. One command, run nightly by
a timer:

```sh
python tools/backup.py                  # build, verify, upload, prune
python tools/backup.py --list
python tools/backup.py --verify         # re-check the newest stored backup
python tools/backup.py --restore familiar-20260826T031500Z.tar.gz --into /tmp/r
```

Configure it with two lines in `.env` — `BACKUP_DEST` (an `s3://bucket/prefix`
for anything S3-compatible, or a path on another machine) and `BACKUP_KEEP`
(default 14). S3 needs `pip install boto3` and the standard `AWS_*` variables;
a non-Amazon provider also needs `BACKUP_S3_ENDPOINT`. Install the timer with
`sudo cp deploy/familiar-backup.{service,timer} /etc/systemd/system/` and
`sudo systemctl enable --now familiar-backup.timer`.

Three things about it are deliberate:

- **`app.db` is snapshotted, not copied.** SQLite runs in WAL mode here, so
  most recent writes live in `app.db-wal`; copying `app.db` alone yields a
  database that opens cleanly and is missing the evening. The snapshot goes through sqlite's online backup API,
  so the service keeps running while it happens.
- **Files first, then the database.** They cannot be captured in the same
  instant, so the question is which inconsistency survives. This order means a
  table deleted mid-run leaves an orphan directory and a table created mid-run
  leaves a row whose directory is recreated on first write. The other order
  leaves a row pointing at files that were never captured.
- **Every run verifies what it wrote** before storing it: the archive is
  reopened, the database inside integrity-checked, and every table row matched
  to a directory. A run that cannot be verified exits non-zero and stores
  nothing. `data/last-backup.json` holds the last report, success or failure —
  a backup regime's characteristic failure is not a loud one, it is silence for
  four months.

Restoring never happens in place: `--restore` refuses a directory with anything
in it, so an operator looks at what came back before moving it over a live
service.

## The puppet

A body that is not a browser — the cat with servos in it — joins the same state
bus the screen does, and there is a build guide for one in
[`pi/README.md`](../pi/README.md) (a Pi, a PCA9685 and six servos, £45–70).

Pairing is two steps, because a body cannot follow an invite link and type its
name:

1. The DM mints a short code in *Running the table → Bodies*. It is 8
   characters from an alphabet with no `0`/`O`, `1`/`I`/`L` or `U` — it gets
   read out across a table — and it is single-use and dead in fifteen minutes.
2. The Pi redeems it once: `python body_client.py --pair WXYZ-2345 --host
   https://your-host`. It gets a long-lived token of its own, stored 0600 in
   `~/.config/familiar/device.json` along with which table it belongs to.

After that the Pi needs no arguments, and the bus accepts two credentials: the
member cookie a browser already has, or `Authorization: Bearer <device token>`.
Browsers cannot set headers on a WebSocket and a Pi has no cookie jar, so the
two paths never overlap.

Three properties worth knowing, all of which follow from *where the credential
lives* — a file inside a physical object in somebody's house:

- **Only the hash is stored.** A stolen database does not hand over anybody's
  puppet.
- **A device listens; it does not drive.** Everything a paired body sends is
  ignored rather than fanned out, so a cat that walks off cannot push states at
  the table it came from. When something does need the other direction (a
  button on the puppet), it is one line in `ws_seat`.
- **Revocation is per device.** *Forget* kills one body's token on its next
  connect and disturbs nothing else — no shared password to rotate in five
  places.

## Useful endpoints

- `POST /api/t/<slug>/ask` — `{"question": "..."}` → `{"answer": "..."}`.
- `POST /api/t/<slug>/ask-stream` — same request, but the answer streams back
  as SSE (`data: {"delta": "..."}` events, then `{"done": true}`). The web body
  uses this so the familiar starts speaking the first sentence while the rest is
  still being written; it falls back to `/ask` if the stream can't start.
- `GET /api/t/<slug>/config` — the bound familiar's public document, the other
  familiars available to switch to, and who you are at this table.
- `POST /api/t/<slug>/tts` — `{"text": "..."}` → audio bytes in the familiar's
  own voice, or `204 No Content` when no server voice is configured (the browser
  then speaks it locally). Pick the source(s) with `TTS_PROVIDER`.
- `POST /api/t/<slug>/forget` — clears the table's conversation memory.
- `POST /api/t/<slug>/ambient` — `{"text": "..."}` → `{"ok": true}`. Ambient
  (non-wake-word) speech, sent only when the "record table talk" toggle is
  on (see [Session memory: ambient capture](#session-memory-ambient-capture)
  above); folds into that table's `game-context.md` in the background.
- `GET /api/t/<slug>/sessions` / `GET /api/t/<slug>/sessions/<date>/timeline` —
  dates with a recorded timeline, and one date's events (amendment-resolved).
- `POST /api/t/<slug>/sessions/<date>/timeline/<event_id>/amend` —
  `{"text", "roll": {"character","outcome"} | null, "rest": "long"|"short"|null,
  "weather": "..."|null}` → the event's updated shape. A tag key that is
  *absent* is left alone; one present as `null` is cleared. Appends a correction; never rewrites the original capture. Who made it
  comes from your session, not from the request body.
- `GET /api/t/<slug>/cast` / `POST /api/t/<slug>/cast/<npc_id>/amend` — the
  auto-maintained NPC list, and the same amend-without-overwriting pattern.
- `GET /api/t/<slug>/locations` / `POST /api/t/<slug>/locations/<place_id>/amend`
  — the auto-maintained list of places. Same shape, same rules, same code: one
  `Roster` class serves both.
- `GET /api/t/<slug>/export` — **owner or DM.** Zip of everything this table
  has recorded. See above.
- `POST /api/t/<slug>/delete` — **owner only.** Irreversible; requires the slug
  echoed back. `POST /api/t/<slug>/leave` gives up your own membership.
- `POST /api/account/delete` — password required; deletes the tables you own.
- `GET /api/t/<slug>/search?q=&kind=&limit=` — full-text search over events,
  cast, places and past questions. `kind` filters to one of `event`, `npc`,
  `place`, `qa`; `counts` in the response always describes the *whole* search,
  so the filter chips don't lie about what is behind them. Snippets mark the
  match with two control characters rather than HTML tags — the client builds
  `<mark>` elements through the DOM, because nothing transcribed from a room
  goes near `innerHTML`.
- `GET /api/t/<slug>/calendar` — the in-game clock: how many days the party
  has lived through, which event ended each one, and the weather recorded on it.
  Derived from long rests on every call; nothing about it is stored.
- `GET /api/t/<slug>/stats` — `{"characters": {"<name>": {"success","failure","crit","nat1"}}, ...}`,
  computed fresh from the current (amendment-aware) timeline on every call.
- `POST /api/t/<slug>/sessions/<date>/reprocess` — **DM only.** Regenerates
  `game-context.md` and `cast.json`'s auto text from `<date>` forward, using
  amended event text. Explicit, not automatic — see above.
- `GET /api/t/<slug>/usage` — **DM only.** This month's tokens, characters,
  estimated cost and cache hit rate. See above.
- `GET /api/t/<slug>/persona` / `PUT` — **DM only.** The familiar's persona and
  the campaign notes. This is what the den's editor calls.
- `POST /api/t/<slug>/familiar` — **DM only.** Bind a different familiar.
- `GET /api/t/<slug>/invite` — **DM only.** The players' invitation link.
- `GET /api/t/<slug>/devices` / `POST` / `DELETE .../<device_id>` — **DM only.**
  The puppets paired to this table, minting a pairing code, and forgetting one.
- `POST /api/pair` — `{"code": "WXYZ-2345"}` → a device token, the table it
  belongs to and the bus path to hold. Unauthenticated, like the join link: the
  code *is* the credential.
- `GET /api/signup` — `{"mode": "open"|"code"|"closed"}`, so the signup form
  knows whether to ask for an invitation code. Public, and never says what the
  code is.
- `POST /api/signup`, `POST /api/login`, `POST /api/logout`, `GET /api/me`,
  `POST /api/tables` — accounts and tables. `POST /api/join/<token>` is how a
  player accepts an invitation.

Every `/api/t/<slug>/…` route requires membership of that table; the ones marked
**DM only** additionally require the DM role. State-changing calls must send
`Content-Type: application/json` — that requirement is the CSRF defence, so a
form post gets a `415` rather than being honoured.
