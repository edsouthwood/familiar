#!/usr/bin/env python3
"""Run, list, verify or restore a backup of the whole service.

Everything interesting is in `backup.py` at the repo root; this is the part an
operator and a systemd timer talk to. It reads `.env` for `BACKUP_DEST`,
`BACKUP_KEEP` and `DATA_DIR` the same way the server does, so the nightly unit
needs no configuration of its own.

    python tools/backup.py                    # one run: build, verify, upload, prune
    python tools/backup.py --dest /mnt/backup # ...to somewhere else
    python tools/backup.py --list             # what is stored
    python tools/backup.py --verify           # check the newest stored backup
    python tools/backup.py --restore <name> --into /tmp/restored

Exit status is 0 only when a good backup was produced (or the requested check
passed). Anything else is 1, which is what makes `OnFailure=` in the systemd
unit -- and any monitoring that watches exit codes -- mean something.
"""

import argparse
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import backup as backup_lib  # noqa: E402
import server  # noqa: E402  (side effect: load_env() populates os.environ from .env)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _warn_if_same_disk(data_dir: Path, dest) -> None:
    """A copy on the same filesystem is not a backup. Say so, once, loudly."""
    if not isinstance(dest, backup_lib.LocalDestination):
        return
    try:
        root = dest.root if dest.root.exists() else dest.root.parent
        if root.stat().st_dev == Path(data_dir).stat().st_dev:
            print("warning: the backup destination is on the same filesystem as "
                  "the data it is backing up. That survives a mistake, not a "
                  "disk. Point BACKUP_DEST at object storage or another machine.",
                  file=sys.stderr)
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR") or str(BASE / "data"),
                    help="what to back up (default: DATA_DIR, else ./data)")
    ap.add_argument("--dest", default=os.environ.get("BACKUP_DEST", ""),
                    help="s3://bucket/prefix, file:///path, or a plain path "
                         "(default: BACKUP_DEST)")
    ap.add_argument("--keep", type=int,
                    default=int(os.environ.get("BACKUP_KEEP") or backup_lib.DEFAULT_KEEP),
                    help=f"how many to keep (default: {backup_lib.DEFAULT_KEEP}; 0 = all)")
    ap.add_argument("--list", action="store_true", help="list stored backups and stop")
    ap.add_argument("--verify", metavar="NAME", nargs="?", const="",
                    help="verify a stored backup (default: the newest) and stop")
    ap.add_argument("--restore", metavar="NAME", help="restore one backup")
    ap.add_argument("--into", metavar="DIR", help="where --restore unpacks (must be empty)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    try:
        dest = backup_lib.destination_for(args.dest)
    except backup_lib.BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.list:
            names = dest.list()
            print("\n".join(names) if names else f"no backups at {dest.describe()}")
            return 0

        if args.verify is not None:
            name = args.verify or (dest.list() or [None])[-1]
            if not name:
                print(f"error: nothing stored at {dest.describe()}", file=sys.stderr)
                return 1
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                local = dest.get(name, Path(tmp) / name)
                report = backup_lib.verify_archive(local)
            print(json.dumps(report, indent=2) if args.json else
                  f"{name}: ok — {_plural(report['tables'], 'table')}, "
                  f"{_plural(report['users'], 'user')}"
                  + "".join(f"\n  warning: {w}" for w in report["warnings"]))
            return 0

        if args.restore:
            if not args.into:
                print("error: --restore needs --into <empty dir>", file=sys.stderr)
                return 1
            report = backup_lib.restore(dest, args.restore, Path(args.into))
            print(json.dumps(report, indent=2) if args.json else
                  f"restored {report['restored']} into {report['into']} "
                  f"({_plural(report['tables'], 'table')}). Look at it before you move "
                  f"it into place, and stop the service first.")
            return 0

        _warn_if_same_disk(data_dir, dest)
        report = backup_lib.run_backup(data_dir, dest, keep=args.keep)
        backup_lib.write_status(data_dir, report)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"{report['archive']} → {report['destination']} "
                  f"({_human(report['bytes'])}, {_plural(report['tables'], 'table')}, "
                  f"{_plural(report['files'], 'file')})")
            for w in report["warnings"]:
                print(f"  warning: {w}")
            if report["pruned"]:
                print(f"  pruned {len(report['pruned'])} old: "
                      f"{report['pruned'][0]} … {report['pruned'][-1]}")
        return 0

    except backup_lib.BackupError as exc:
        # A failed run is written down too. The status file is what a human
        # looks at months later, and "it stopped working in March" has to be
        # visible there rather than only in a journal that has rotated away.
        backup_lib.write_status(data_dir, {
            "ok": False, "error": str(exc),
            "destination": dest.describe(),
            "finished_at": backup_lib._now().isoformat(timespec="seconds"),
        })
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
