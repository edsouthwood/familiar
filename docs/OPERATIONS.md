# Running a familiar server — an operator's runbook

For tier 3: the familiar on an always-on box (a VPS, a home server, a Pi 4)
that players reach from their phones. Everything a human has to *do* to keep it
alive, in the order you will need it. The [README](../README.md) gets you
started; [HOW-IT-WORKS.md](HOW-IT-WORKS.md) explains why it is built the way it
is. This file assumes you have forgotten all of that.

One paragraph of orientation. The familiar is **one Python process** (Flask +
gunicorn) behind **Caddy**, holding **one SQLite database** and a **directory of
prose per table**. There is no queue, no cache, no second service. Almost every
operational question below is really a question about one of those four things.

---

## 1. The shape of it

```
   the table's iPad ─┐
   a player's phone ─┼─ https ─→ Caddy ─→ gunicorn (ONE worker, 64 threads)
   the puppet (Pi) ──┘   wss              │  server.py
                                          ├─→ data/app.db      accounts, tables,
                                          │                    members, usage, search
                                          └─→ data/tables/<id>/ the campaign itself
                                                               (markdown + json)
        outbound: an LLM (Gemini / Claude / a PC on your LAN)
                  TTS is in-process — no network, no quota
```

**Where everything lives**

| Path | What it is |
|---|---|
| the checkout (e.g. `/opt/familiar`) | the app: code, `.env`, `static/`, the tracked `*.example.md` defaults |
| `data/app.db` | accounts, tables, memberships, usage, devices, and the search index |
| `data/tables/<table id>/` | one table's whole life: `campaign.md`, `game-context.md`, `cast.json`, `locations.json`, `party.json`, `sessions/`, `familiars/` |
| `data/last-backup.json` | the last backup run's report, success or failure |
| `voices/` | Piper voices (`.onnx`) |
| `pi/body_client.py` | the puppet. Runs on the Pi, not here |

The two roots are deliberately separate: the checkout is the application,
`DATA_DIR` is the tables. You can move the second to another volume with one
line in `.env` and nothing else changes — and moving a table from your laptop
(tier 1) to the server is copying `data/` across.

> No `sqlite3` command-line tool? Either `sudo apt install sqlite3`, or every
> `sqlite3 data/app.db '<sql>'` below has this equivalent:
>
> ```sh
> .venv/bin/python -c "import sqlite3,sys;print(*sqlite3.connect('data/app.db').execute(sys.argv[1]),sep='\n')" '<sql>'
> ```

---

## 2. Standing it up from nothing

You need a domain name with an A record pointing at the box, and ports 80 and
443 open. HTTPS is not optional here: browsers only allow the microphone on a
secure origin.

### With Docker (simplest)

```sh
git clone <this repo> familiar && cd familiar
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_hex(32))'   # → SECRET_KEY
$EDITOR .env          # SECRET_KEY, GEMINI_API_KEY, SIGNUP_CODE, and DOMAIN=
docker compose --profile https up -d
```

Caddy fetches the certificate on first start. Logs: `docker compose logs -f`.
In the commands below, run the `.venv/bin/python …` ones as
`docker compose exec familiar python …` instead.

### With systemd

```sh
sudo useradd --system --create-home --home-dir /opt/familiar familiar
sudo -u familiar git clone <this repo> /opt/familiar
cd /opt/familiar && sudo -u familiar ./setup.sh
$EDITOR .env          # add HOST=127.0.0.1 and SIGNUP_CODE (see §3)

sudo cp deploy/familiar.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now familiar
```

Then Caddy: put your domain in `deploy/Caddyfile-snippet`, append it to
`/etc/caddy/Caddyfile`, `sudo systemctl reload caddy`. WebSockets need no
special configuration — Caddy proxies them transparently, and `wss://` is
selected automatically on an HTTPS site.

Make the first account in a browser — on a fresh install `/` sends you to the
signup form — then *Start a new table* in the den. **Set `SIGNUP_CODE` before
anybody else can reach the address** (§5).

---

## 3. The dials, all of them in `.env`

`.env.example` is the annotated version; this is the map. Real environment
variables (systemd's `EnvironmentFile`, docker's `env_file`) always beat the
file.

| Group | Keys | Notes |
|---|---|---|
| **Required** | `SECRET_KEY` | signs the login cookies. **Changing it signs everybody out.** There is no generated fallback on purpose — one would log everybody out on every restart, silently |
| | `DATA_DIR` | where tables live. Defaults to `./data` |
| **Network** | `HOST`, `PORT` | `HOST=127.0.0.1` behind Caddy, so the only way in is HTTPS |
| **Gate** | `SIGNUP_CODE` | who may create an account (§5). Players never need one |
| **Brain** | `LLM_PROVIDER`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `LOCAL_LLM_URL` | a comma-separated chain, tried left to right: `local,gemini` asks the PC in your house first and falls through when it is off |
| | `CLAUDE_MODEL`, `GEMINI_MODEL` | which model answers |
| | `FOLD_INS_PER_HOUR` | the brake on ambient summarising, per table (default 12) |
| **Voice** | `TTS_PROVIDER`, `PIPER_VOICE`, `POCKET_VOICE` | a chain again. In-process, so no quota and no network |
| **Backups** | `BACKUP_DEST`, `BACKUP_KEEP`, `AWS_*`, `BACKUP_S3_ENDPOINT` | §4 |

After any change: `sudo systemctl restart familiar` (or `docker compose up -d`).

---

## 4. Every night, without you

One timer, one command, and one file to look at afterwards.

```sh
sudo cp deploy/familiar-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now familiar-backup.timer
```

It fires at **03:20** (plus up to 20 minutes of jitter), and `Persistent=true`
means a night the box was off is made up as soon as it is back. Each run builds
a `.tar.gz`, **verifies it**, uploads it, then deletes everything past
`BACKUP_KEEP` (default 14). With Docker, run the same command from the host's
cron: `docker compose exec -T familiar python tools/backup.py`.

**Is it working?** Three answers, cheapest first:

```sh
cat data/last-backup.json                  # the last run, success or failure
.venv/bin/python tools/backup.py --list    # what is actually stored
systemctl list-timers familiar-backup      # when it next fires
```

A backup regime does not fail loudly; it fails by going quiet for four months.
`last-backup.json` is written for failed runs too, and a failed run exits
non-zero — which is what makes `OnFailure=` in the unit worth wiring to
something once you have something to wire it to.

**Restore one, on a quiet Tuesday, before you need to.**

```sh
.venv/bin/python tools/backup.py --restore familiar-20260826T031500Z.tar.gz --into /tmp/r
ls /tmp/r/tables/*/                        # somebody's campaign, in plain text
sqlite3 /tmp/r/app.db 'select slug from tables;'
```

Restoring never happens in place: `--restore` refuses a directory with anything
in it. To actually go back, stop the service, move `data/` aside, move the
restored directory in, start the service, and rebuild the search index (§5).

Three things about backups worth knowing before you need them:

- **`app.db` is snapshotted, not copied.** It runs in WAL mode, so most recent
  writes live in `app.db-wal`. A `cp` of `app.db` produces a file that opens
  cleanly and is missing the evening.
- **A destination on the same disk is not a backup.** The tool warns when it
  notices. It survives a mistake, not a dead disk.
- **The archive is just files.** A table's whole history is a few hundred
  kilobytes of prose; you can read a restored campaign in `less`.

---

## 5. Things you do by hand

### Let somebody make an account

DMs need accounts; players don't (they join with the DM's invite link). Set
`SIGNUP_CODE` in `.env`, then restart. Give the code to whoever you want in;
change it and the old one is dead. `open` (the literal word) lets anyone in —
and spends your API key on their tables; unset takes nobody new. The signup
form asks the server which of the three it is and draws itself accordingly.

```sh
echo 'SIGNUP_CODE=dragons-2026' >> .env
sudo systemctl restart familiar
curl -s https://familiar.example.com/api/signup      # {"mode":"code"}
```

### Pair a puppet

In the browser: the table's page → *Running the table* → **Bodies** → **Pair a
puppet**. A code appears (`WXYZ-2345`) with the exact line to run on the Pi:

```sh
python body_client.py --pair WXYZ-2345 --host https://familiar.example.com
```

The code works **once** and dies after fifteen minutes. The Pi stores its own
token in `~/.config/familiar/device.json` (0600) and needs no arguments ever
again. Full build guide, including the parts list: `pi/README.md`.

### Forget a puppet (it was lost, sold, or is misbehaving)

Same panel, **Forget**. Its token stops working on its next connect and nothing
else at the table is disturbed. If you also have the Pi in your hand,
`body_client.py --forget` clears its copy; the browser half is the one that
matters.

### Look at what a table has spent

The DM sees it in **the den**, under their table. From here:
`GET /api/t/<slug>/usage`, or `sqlite3 data/app.db 'select * from usage;'`.

### Rebuild the search index

Search is an *index*, never a source of truth — every row in it is derived from
a file. So it is always safe to rebuild, and rebuilding is the fix for every
search-shaped complaint:

```sh
.venv/bin/python tools/reindex_search.py                 # everything
.venv/bin/python tools/reindex_search.py --table <slug>  # one table
```

Run it after hand-editing, copying in or restoring a table's files —
**nothing does this automatically.**

### Fix a session the familiar got wrong

`POST /api/t/<slug>/sessions/<date>/reprocess` (DM only, and there is a button
in the timeline view) regenerates the memory and cast from that date forward
using the corrected event text. It costs LLM calls and it cascades into later
sessions.

### Get a table's data out, or destroy it

| | | In the den? |
|---|---|---|
| `GET /api/t/<slug>/export` | owner or DM. A zip of everything, plus a README naming each file | yes |
| `POST /api/t/<slug>/delete` | **owner only**, slug echoed back, irreversible | yes |
| `POST /api/t/<slug>/leave` | a player's own membership. Their corrections stay | yes |
| `POST /api/account/delete` | password required. Takes the tables you *own*, leaves tables you merely play at | no — the API only |

To delete somebody's account for them, they can send the request from their
own signed-in browser's console:

```js
fetch("/api/account/delete", {method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({password: "..."})})
```

Deletion spans three stores — rows, files, and the FTS index, which takes no
foreign key. `server.delete_table_completely()` is the only thing that should
touch them.

### Reset a forgotten password

There is no email, so no reset link. From the box:

```sh
.venv/bin/python -c "
import server; u = server.db.user_by_email('dm@example.com'); server.db.set_password(u['id'], 'a-new-password')"
```

---

## 6. When something is wrong

| Symptom | Look at | Usually |
|---|---|---|
| Site down | `systemctl status familiar`, `journalctl -u familiar -n 100` | a bad `.env`, or a dependency that moved under a Python upgrade |
| TLS / certificate | `journalctl -u caddy --since yesterday \| grep -i certificate` | DNS not pointing here yet, or port 80 closed (Let's Encrypt needs it) |
| Everyone signed out at once, **and login returns 500** | `grep SECRET_KEY .env` | the key is missing or changed. The site looks *fine* — pages render, no error is logged — but every cookie silently fails to verify, and the 500 only appears when somebody tries to sign in again |
| Answers are slow | the first provider in `LLM_PROVIDER` | a `local` provider pointing at a PC that is off costs a ~2s timeout on every question before failover. Drop it from the chain or turn the PC on |
| Table talk stops filling the timeline | `journalctl -u familiar \| grep fold-ins/hour` | the hourly fold-in cap (`FOLD_INS_PER_HOUR`). It resets within the hour |
| Puppet paired but still dials the old address | `grep -r FAMILIAR_WS_URL /etc/familiar-body.env ~/familiar-body/.env` on the Pi | a leftover `FAMILIAR_WS_URL`. Pairing outranks it and the client says it is ignoring one; delete the line anyway |
| Puppet is running fine and logging nothing | `systemctl show familiar-body -p Environment` | Python block-buffers stdout when it is a pipe, so a *healthy* client is silent. `Environment=PYTHONUNBUFFERED=1` in the unit fixes it |
| Puppet will not connect | `journalctl -u familiar-body -f` on the Pi | not paired, paired as a different unix user, or the token was forgotten server-side. See "If it will not connect" in `pi/README.md`, and `pi/check-puppet.sh` |
| Puppet connects but never moves | nothing is driving the bus | a device **listens but does not drive** by design. Wake the familiar in a browser at that table |
| Search finds nothing / stale hits | — | reindex (§5). It is derived; rebuilding is always safe |
| Backups stopped | `cat data/last-backup.json` | credentials, a full bucket, or the timer was never enabled |
| The iPad mishears its name | — | iOS forces every browser onto Apple's speech engine, which is weaker. Desktop Chrome uses Google's and is much better. Add the mishearings to the familiar's wake words |

**Liveness, from the box:**

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8321/login.html    # 200 = alive
```

---

## 7. The rules that are load-bearing

Things that look like preferences and are not. Each of these has a reason
written where it is enforced; this is the index.

- **One gunicorn worker. Never `-w 2`.** Per-table state lives in `TableRuntime`
  objects *in process*: two workers means two registries, so two bodies at the
  same table could land in different processes and never see each other. It also
  matters for data integrity — timeline event ids are positional, computed by
  counting lines under a process-local lock, and amendments key on that index.
  The escape hatch when one process is not enough is the **gevent worker** (still
  one process), not a second worker.
- **`SECRET_KEY` is permanent, and losing it is quiet.** Check it first
  whenever "everyone got logged out".
- **Whatever key this box holds pays for every table on it.** Keep
  `SIGNUP_CODE` set to something only your people know.
- **The search index is derived. Nothing else in the data model is.**
- **Prompt caching needs a long enough prompt.** On Claude the cacheable prefix
  has to reach ~1024 tokens; below that it fails silently, with no error and no
  discount. Watch `cache_hit_rate` in the den.

---

## 8. The five-minute version

```sh
sudo systemctl status familiar caddy          # is it up
cat data/last-backup.json                     # did last night work
.venv/bin/python tools/backup.py --list       # is anything actually stored
journalctl -u familiar --since '1 hour ago' | grep -i error
sqlite3 data/app.db 'select slug, name from tables;'
```

If those five are boring, nothing is wrong.
