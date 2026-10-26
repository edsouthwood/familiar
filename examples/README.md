# Example familiars

Two finished familiars to start from, or to read before writing your own.

| | Shell | Personality |
|---|---|---|
| `whiskers/` | cat | vain, lazy, secretly devoted |
| `pellinore/` | owl | courteous, precise, a retired librarian |

Each is the same shape the server keeps on disk: `familiar.json` (name, wake
words, language, shell, sound pack) and `persona.md` (who they are).

**The easy way:** create a familiar in the browser, give it the same name and
shell, and paste `persona.md` into its persona box.

**Or copy it in** — a familiar is just a folder in the table's data directory:

```bash
cp -r examples/familiars/pellinore data/tables/<table id>/familiars/
```

Neither has campaign knowledge built in. That lives in the table's campaign
notes, which the DM edits in the browser, so the same familiar can sit in on
any game.

The owl has no sound pack yet — drop clips into `static/sounds/owl/` to give
Pellinore a hoot.
