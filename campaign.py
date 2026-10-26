"""The campaign notes: a hand-editable markdown file, read as named fields.

`campaign.md` is the DM's own document. Its preamble says "Edit this file
freely", and it ships in the export zip. So it stays a file a person can open in a text editor, and this
module is the seam that lets a form edit the same thing without either side
owning it exclusively.

That constraint drives every rule below. The parser is asked to read prose typed
by a tired DM at eleven at night, so:

* **it never raises.** There is no such thing as a malformed campaign note.
* **it never drops bytes.** Every non-heading line of the input comes back in
  some field. Text under a heading nobody recognises is preserved verbatim
  rather than helpfully tidied away, because "helpfully" is how you lose
  somebody's session notes.
* **it fails closed on secrets.** A `## DM only` heading typed out of habit is
  routed to `dm_only_stray`, which `reference()` never returns and `render()`
  never writes back into `campaign.md`.

Three sections that used to be in the template are gone from FIELDS, and their
content is preserved rather than deleted -- see RETIRED. They were invitations
to duplicate by hand what the machine now maintains: the party is `party.json`,
"people we've met" is `cast.json`, and "last session recap" is the timeline.
"""

import re

# The editable fields, in the order the form shows them and the file writes
# them. "Where we are" is first because it is the thing a DM changes most, and
# on a phone the field you want should not be a scroll away.
FIELDS = (
    ("where", "Where we are"),
    ("setting", "Setting"),
    ("house_rules", "House rules"),
    ("other", "Other notes"),
)

FIELD_KEYS = tuple(key for key, _ in FIELDS)

# What each section is for, in the DM's terms rather than the parser's.
#
# Here rather than in the browser because a hint that lives beside the form is
# a hint that goes stale the first time a field is renamed, and these have to
# describe the same four sections campaign.example.md describes. They say what
# the familiar *does* with the text, because that is the part a DM cannot guess
# and the part that changes what they write: "the notes win over the book" is
# the reason to bother typing a house rule into a box at all.
HINTS = {
    "where": "One or two lines on the scene the party is standing in right now. "
             "The section you will change most, and the one the familiar leans "
             "on hardest when somebody asks it what is going on.",
    "setting": "The world and its tone — region, era, who holds power, what the "
               "party has got themselves caught up in. Written once and mostly "
               "left alone.",
    "house_rules": "Anything your table does differently from the book. Worth "
                   "typing out: the familiar is told that these beat the printed "
                   "rule, so this is how it stops confidently quoting the manual "
                   "at you.",
    "other": "Anything that does not belong above. Sections from an older "
             "campaign.md land here too, tagged with where they came from, so "
             "you can check them against whatever owns them now and delete them.",
}

assert set(HINTS) == set(FIELD_KEYS), "every field says what it is for"

# Headings the template used to ask for, now maintained elsewhere. Content
# under one of these is kept verbatim under Other notes, tagged so the DM can
# see where it came from and delete it once they have checked it against the
# thing that now owns it. Nothing here is ever discarded automatically.
RETIRED = {
    "the party": "party.json",
    "people we've met": "cast.json",
    "people weve met": "cast.json",       # a stray apostrophe is not a reason to lose text
    "last session recap": "the timeline",
}

DM_ONLY = "dm only"

# The document's own preamble. render() re-emits it and parse() drops it again,
# which is what makes parse(render(f)) == f hold.
_PREAMBLE = (
    "# Campaign notes\n"
    "\n"
    "> Edit this file freely -- it is reloaded on every question, no restart\n"
    "> needed, and the hub's form reads and writes these same sections.\n"
    "> Secrets do NOT belong here: they live in dm-only.md, which the memory\n"
    "> fold-in never opens.\n"
)

_HEADING_RE = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)


def _key(heading: str) -> str:
    """A heading reduced to something comparable: case and inner whitespace
    are not the DM's problem."""
    return re.sub(r"\s+", " ", heading.strip()).casefold()


_BY_HEADING = {_key(label): key for key, label in FIELDS}


def parse(text: str) -> dict:
    """Split campaign notes into fields. Never raises; never drops a line.

    Returns every key in FIELD_KEYS plus `dm_only_stray`, each a (possibly
    empty) string.

    A `## ` heading nobody recognises keeps its heading and its body, demoted
    to `###` and appended to Other notes. The demotion is what stops the next
    parse from reading it as a section again, so a round trip is stable.
    """
    text = (text or "").replace("\r\n", "\n")
    out = {key: [] for key in FIELD_KEYS}
    out["dm_only_stray"] = []

    matches = list(_HEADING_RE.finditer(text))
    preamble = text[: matches[0].start()] if matches else text

    # Anything before the first heading. The `#` title and the `>` blockquote
    # are this module's own boilerplate, re-emitted by render(); anything else
    # is somebody's prose and is kept.
    kept = [line for line in preamble.splitlines()
            if not line.lstrip().startswith(("#", ">"))]
    if any(line.strip() for line in kept):
        out["other"].append("\n".join(kept).strip())

    for i, match in enumerate(matches):
        heading = match.group(1)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end():end].strip("\n")
        key = _key(heading)

        # A prefix match, not equality: "## DM only:", "## DM only notes" and
        # friends must all fail closed into the stray bucket. The old parser's
        # regex was a case-insensitive prefix and this replaced it; equality
        # here is how a trailing colon leaks secrets to every player.
        if key.startswith(DM_ONLY):
            out["dm_only_stray"].append(body.strip())
        elif key in _BY_HEADING:
            out[_BY_HEADING[key]].append(body.strip())
        elif key in RETIRED:
            out["other"].append(
                f"### {heading.strip()}\n"
                f"_(kept from your old notes; {RETIRED[key]} maintains this now)_\n"
                f"\n{body.strip()}".rstrip())
        else:
            out["other"].append(f"### {heading.strip()}\n\n{body.strip()}".rstrip())

    return {key: "\n\n".join(part for part in parts if part).strip()
            for key, parts in out.items()}


def render(fields: dict) -> str:
    """Fields back to a markdown document, in canonical order with canonical
    headings. Empty fields are skipped rather than written as bare headings --
    a file full of empty sections reads like something went wrong.

    Note `dm_only_stray` is deliberately absent from the output. Rendering is
    how a stray secret leaves campaign.md for good; the caller is responsible
    for having put it somewhere first (see server.api_put_campaign).
    """
    parts = [_PREAMBLE]
    for key, label in FIELDS:
        body = (fields.get(key) or "").strip()
        if body:
            parts.append(f"## {label}\n\n{body}\n")
    return "\n".join(parts)


def reference(fields: dict) -> str:
    """What a prompt is allowed to see: the four fields, never the secrets.

    Separate from render() because they answer different questions. render()
    produces a file for a human to open; this produces context for a model, and
    the difference that matters is that this one cannot grow a DM-only section
    by accident -- it enumerates FIELDS and nothing else.
    """
    parts = []
    for key, label in FIELDS:
        body = (fields.get(key) or "").strip()
        if body:
            parts.append(f"## {label}\n\n{body}")
    return "\n\n".join(parts)


def is_empty(fields: dict) -> bool:
    return not any((fields.get(key) or "").strip() for key in FIELD_KEYS)
