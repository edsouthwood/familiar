#!/usr/bin/env python3
"""Rebuild the full-text search index from the files it is derived from.

The index (the `search` table in data/app.db) holds no original data. Every
row in it is a copy of something already written under data/tables/<id>/ --
timeline events and their amendments, cast.json, locations.json, and the Q&A
session logs. That is what lets the live indexing be best-effort: a write that
fails costs a search miss, and this script is how the miss gets fixed.

Run it when:

  * search results look stale or incomplete;
  * you have hand-edited, copied in or restored a table's files.

It is safe to run at any time and as often as you like: each table's index is
dropped and rebuilt in one pass, and nothing else is touched. It makes no LLM
calls and costs nothing.

    python tools/reindex_search.py                 # every table
    python tools/reindex_search.py --table <slug>  # just one
    python tools/reindex_search.py --dry-run       # count, change nothing
"""

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import server  # noqa: E402  (side effect: load_env() populates os.environ from .env)
from tenancy import Table  # noqa: E402


def index_table(table, dry_run: bool = False) -> dict:
    """Rebuild one table's index. Returns a count per kind."""
    counts = {"event": 0, "npc": 0, "place": 0, "qa": 0}
    if not dry_run:
        server.db.clear_index(table.id)

    for date in server._session_dates(table):
        for event in server._resolve_timeline_events(table, date):
            counts["event"] += 1
            if not dry_run:
                server._index_event(table, date, event)

    for roster in (server.CAST, server.LOCATIONS):
        entry_ids = list(roster.load(table)[roster.key])
        counts[roster.noun] += len(entry_ids)
        if not dry_run:
            roster.index(table, entry_ids)

    for path in sorted(table.sessions_dir.glob("*.log")):
        # The Q&A logs are "<date>.log"; the timeline is
        # "<date>-timeline.log" and the amendments "<date>-amendments.log",
        # both of which are indexed above and must not be read as Q&A.
        date = path.stem
        if "-" in date[10:]:
            continue
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            question, answer = rec.get("q") or "", rec.get("a") or ""
            if not (question or answer):
                continue
            counts["qa"] += 1
            if not dry_run:
                server._index_qa(table, date, index, question, answer)

    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", default="", help="slug of one table (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be indexed, change nothing")
    args = parser.parse_args()

    rows = ([server.db.table_by_slug(args.table)] if args.table
            else server.db.all_tables())
    if not rows or rows[0] is None:
        raise SystemExit(f"no table with slug {args.table!r}" if args.table
                         else "no tables exist yet -- create one in the browser first")

    total = 0
    for row in rows:
        table = Table(row, server.DATA_DIR, server.BASE)
        counts = index_table(table, args.dry_run)
        n = sum(counts.values())
        total += n
        summary = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
        print(f"{row['slug']}: {n} document(s)" + (f" ({summary})" if summary else ""))

    verb = "would index" if args.dry_run else "indexed"
    print(f"{verb} {total} document(s) across {len(rows)} table(s)")


if __name__ == "__main__":
    main()
