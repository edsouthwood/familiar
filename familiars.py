"""Familiars as data.

A familiar used to be three .env vars, a persona.md and 135 lines of inline SVG.
This module makes it a directory instead, so a table can own several and swap
between them:

    <data>/familiars/<id>/familiar.json   name, wake words, lang, voice, shell, sounds, owner
                          persona.md      who they are (hand-written)
                          bond.md         standing with the party (auto-maintained)
    <data>/familiars/.active              which one is currently bound

Shells and sound packs are *not* in here. They are shipped application assets
(static/shells/<id>/, static/sounds/<id>/) that a familiar references by name,
the same way it references a piper voice -- many familiars can wear the same
shell, and a shell is not campaign data.

The class takes its roots as arguments rather than reading module-level
constants, because the multi-tenant build gives every table its own instance
pointing at that table's directory.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SHELL = "cat"
DEFAULT_SOUNDS = "cat"
DEFAULT_LANG = "en-GB"

# A familiar id is a slug: it becomes a directory name and appears in URLs.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def slugify_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:64] or "familiar"


@dataclass
class Familiar:
    """One familiar's identity. `dir` is where its persona and bond live."""

    id: str
    dir: Path
    name: str
    wake_words: list[str] = field(default_factory=list)
    lang: str = DEFAULT_LANG
    voice: str = ""          # pocket/piper voice reference; "" = server default
    shell: str = DEFAULT_SHELL
    sounds: str = DEFAULT_SOUNDS

    # The member who summoned this familiar -- a members.id, or "" for a
    # familiar that belongs to the table rather than to a person.
    #
    # In the fiction a familiar is summoned by a player, not by the DM, and the
    # person paying for the service may well be that player. So its persona and
    # its voice are theirs to change, while the campaign, cast and timeline
    # remain the DM's. Unowned ("") is the pre-existing case and stays DM-only,
    # so nothing that already exists loosens its permissions by being upgraded.
    owner: str = ""

    def may_edit(self, member) -> bool:
        """May this member change who this familiar is?

        Its summoner, and nobody else. Not the DM: a familiar's persona and its
        bond with the party are the private business of the player who summoned
        it, and a DM who can read them is a DM who knows what their player told
        the creature in confidence. That is the whole point of a familiar
        belonging to a person rather than to the table.

        An unowned familiar ("") is the table's own, and stays the DM's -- it is
        the pre-existing case, and it would otherwise be editable by nobody.
        """
        if member is None:
            return False
        if not self.owner:
            return member["role"] == "dm"
        return self.owner == member["id"]

    def may_retire(self, member) -> bool:
        """May this member dismiss this familiar?

        Its summoner, or the DM. The DM is permitted here and nowhere else,
        because somebody has to be able to clear away a familiar whose player
        has left the group -- and dismissing a creature is a fact about who is
        at the table, which is the DM's business, while reading its persona is
        not. Deliberately the *only* seam where the two differ.
        """
        if member is None:
            return False
        return self.may_edit(member) or member["role"] == "dm"

    @property
    def persona_path(self) -> Path:
        return self.dir / "persona.md"

    @property
    def bond_path(self) -> Path:
        return self.dir / "bond.md"

    def public(self) -> dict:
        """The identity document the browser renders itself from. Never secrets.

        `wakeVariants` keeps its old name: the browser has always called it that
        (static/familiar.js), and renaming it buys nothing.
        """
        return {
            "id": self.id,
            "name": self.name,
            "wakeVariants": self.wake_words or [self.name.lower()],
            "lang": self.lang,
            "shell": self.shell,
            "sounds": self.sounds,
            "owner": self.owner,
        }


class Familiars:
    """The familiars belonging to one table.

    `data_root` is that table's data directory; `app_root` is the application
    root, used only to find the tracked persona.example.md fallback so a fresh
    install has something to say before anyone writes a persona.
    """

    def __init__(self, data_root: Path, app_root: Path):
        self.dir = Path(data_root) / "familiars"
        self.app_root = Path(app_root)
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- reading

    def _read(self, familiar_dir: Path) -> Familiar | None:
        """Load one familiar, or None if the directory isn't one. A malformed
        familiar.json is skipped rather than raised: one bad directory must not
        take the whole familiar list (and with it the familiar page) down."""
        path = familiar_dir / "familiar.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        name = str(doc.get("name") or familiar_dir.name).strip()
        words = [
            str(w).strip().lower()
            for w in doc.get("wakeWords") or []
            if str(w).strip()
        ]
        return Familiar(
            id=familiar_dir.name,
            dir=familiar_dir,
            name=name or familiar_dir.name,
            wake_words=words,
            lang=str(doc.get("lang") or DEFAULT_LANG),
            voice=str(doc.get("voice") or ""),
            shell=str(doc.get("shell") or DEFAULT_SHELL),
            sounds=str(doc.get("sounds") or DEFAULT_SOUNDS),
            owner=str(doc.get("owner") or ""),
        )

    def list(self) -> list[Familiar]:
        """Every familiar this table owns, ordered by name."""
        try:
            dirs = sorted(p for p in self.dir.iterdir() if p.is_dir())
        except OSError:
            return []
        found = [self._read(d) for d in dirs]
        return sorted((f for f in found if f), key=lambda f: f.name.lower())

    def get(self, familiar_id: str) -> Familiar | None:
        if not _ID_RE.match(familiar_id or ""):
            return None  # also blocks "..", absolute paths and other traversal
        return self._read(self.dir / familiar_id)

    # ---------------------------------------------------------------- binding

    @property
    def _active_file(self) -> Path:
        return self.dir / ".active"

    def active(self) -> Familiar | None:
        """The currently bound familiar. Falls back to the first one if the
        pointer is missing or dangling, so a hand-deleted familiar leaves the
        table with *a* familiar rather than a blank screen."""
        try:
            wanted = self._active_file.read_text(encoding="utf-8").strip()
        except OSError:
            wanted = ""
        if wanted:
            familiar = self.get(wanted)
            if familiar:
                return familiar
        everyone = self.list()
        return everyone[0] if everyone else None

    def set_active(self, familiar_id: str) -> Familiar:
        familiar = self.get(familiar_id)
        if not familiar:
            raise ValueError(f"no such familiar: {familiar_id!r}")
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._active_file.write_text(familiar.id + "\n", encoding="utf-8")
        return familiar

    def delete(self, familiar_id: str) -> bool:
        """Remove a familiar and everything that made it itself.

        Refuses the last one: a table with no familiar is a screen with nothing
        on it and no way to get anything back, which is a state the UI cannot
        recover from. Retiring the creature you have is done by summoning
        another and binding that.

        Safe against traversal because get() gates on _ID_RE first -- ".." and
        absolute paths never reach the rmtree.
        """
        familiar = self.get(familiar_id)
        if familiar is None or len(self.list()) <= 1:
            return False
        with self._lock:
            shutil.rmtree(familiar.dir, ignore_errors=True)
            # A dangling .active is already handled by active(), which falls
            # back to the first familiar -- so there is nothing to clean up and
            # nothing that can be left pointing at a hole.
        return True

    # ---------------------------------------------------------------- writing

    def create(self, name: str, **fields) -> Familiar:
        """Create a familiar directory. Idempotent on id: an existing familiar
        of the same id is returned untouched rather than overwritten."""
        familiar_id = fields.pop("id", None) or slugify_id(name)
        if not _ID_RE.match(familiar_id):
            raise ValueError(f"bad familiar id: {familiar_id!r}")
        with self._lock:
            familiar_dir = self.dir / familiar_id
            if (familiar_dir / "familiar.json").exists():
                return self._read(familiar_dir)
            familiar_dir.mkdir(parents=True, exist_ok=True)
            doc = {
                "name": name,
                "wakeWords": [w.lower() for w in fields.get("wake_words") or [name.lower()]],
                "lang": fields.get("lang") or DEFAULT_LANG,
                "voice": fields.get("voice") or "",
                "shell": fields.get("shell") or DEFAULT_SHELL,
                "sounds": fields.get("sounds") or DEFAULT_SOUNDS,
                "owner": fields.get("owner") or "",
            }
            (familiar_dir / "familiar.json").write_text(
                json.dumps(doc, indent=2) + "\n", encoding="utf-8"
            )
            persona = fields.get("persona")
            if persona is not None:
                (familiar_dir / "persona.md").write_text(persona, encoding="utf-8")
        return self._read(familiar_dir)

    def update(self, familiar_id: str, **fields) -> Familiar:
        """Write changed fields back to familiar.json. Only known keys are
        accepted, so an unexpected body can't inject arbitrary JSON."""
        familiar = self.get(familiar_id)
        if not familiar:
            raise ValueError(f"no such familiar: {familiar_id!r}")
        allowed = {"name", "wake_words", "lang", "voice", "shell", "sounds", "owner"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        with self._lock:
            path = familiar.dir / "familiar.json"
            doc = json.loads(path.read_text(encoding="utf-8"))
            if "wake_words" in fields:
                doc["wakeWords"] = [str(w).strip().lower() for w in fields.pop("wake_words")]
            doc.update({k: v for k, v in fields.items()})
            path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        return self._read(familiar.dir)

    # ---------------------------------------------------------------- prompts

    def persona_text(self, familiar: Familiar | None) -> str:
        """This familiar's persona, falling back to the tracked example so a
        fresh install still has a personality. Mirrors server._persona_file's
        two-root shape: tenant data first, application default second."""
        for path in (
            familiar.persona_path if familiar else None,
            self.app_root / "persona.example.md",
        ):
            if path and path.exists():
                try:
                    return path.read_text(encoding="utf-8")
                except OSError:
                    continue
        return ""

    def bond_text(self, familiar: Familiar | None) -> str:
        """This familiar's standing with the party -- the familiar-relative half
        of what used to live in game-context.md's '## Party & relationships'.
        Absent until the first fold-in writes it, which is not an error."""
        if not familiar or not familiar.bond_path.exists():
            return ""
        try:
            return familiar.bond_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def write_bond(self, familiar: Familiar | None, text: str) -> None:
        if not familiar:
            return
        try:
            familiar.dir.mkdir(parents=True, exist_ok=True)
            familiar.bond_path.write_text(text.strip() + "\n", encoding="utf-8")
        except OSError:
            pass  # best-effort, like every other memory write
