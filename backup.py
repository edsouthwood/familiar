"""Nightly backups: the whole install, in one file, somewhere else.

Export, delete and consent protect a table's people from whoever runs the
server. This protects everybody from the disk.

What has to be in a backup, and why it is not a `cp`:

* **`app.db` cannot be copied.** SQLite runs in WAL mode here (see
  `db.Database.connect`), so at any moment a large part of the database is in
  `app.db-wal` and not in `app.db` at all -- in this deployment the WAL has
  been seen at 4 MB against an 80 KB main file. Copying the one file yields a
  database that is missing most of the recent writes, and it opens without
  complaint. So the snapshot goes through sqlite's own online backup API,
  which takes a consistent point-in-time copy of a database that is being
  written to, WAL included.
* **The files are the campaign.** Rows say who owns a table and what it has
  spent; the prose under `tables/<id>/` is what anyone would actually miss.
  Both, or it is not a backup.

Ordering inside a run is deliberate: **files first, then the database.** The
two cannot be captured in the same instant, so the question is which
inconsistency is survivable. Files-then-db means a table deleted mid-run leaves
an orphan directory (harmless, ignored) and a table created mid-run leaves a row
whose directory is recreated on first write (harmless). The other order gives a
row pointing at files that were never captured -- a table that restores broken.

Destinations are pluggable in the same shape as the TTS providers: a small
class with four methods, chosen by a URL-ish string. `local` needs nothing and
is what the tests use; `s3` covers S3, Backblaze B2, Cloudflare R2, Hetzner,
MinIO -- anything speaking the S3 API -- and needs `pip install boto3`.

    python tools/backup.py                       # run one, using .env
    python tools/backup.py --dest /mnt/backups   # ...somewhere specific
    python tools/backup.py --list
    python tools/backup.py --restore familiar-20260826T031500Z.tar.gz --into /tmp/r

An untested backup is not a backup, so every run verifies what it just wrote:
the archive is reopened, the database inside it is integrity-checked, and every
table row is matched against a directory. A run that cannot be verified fails
loudly rather than reporting success.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Bumped when the layout inside the archive changes. `restore` refuses a
# format it does not know rather than scattering files in the wrong places.
FORMAT_VERSION = 1

ARCHIVE_PREFIX = "familiar-"
ARCHIVE_SUFFIX = ".tar.gz"
DEFAULT_KEEP = 14

# Everything sqlite keeps beside the database. They are deliberately *not*
# archived: the snapshot is a complete database on its own, and a stale -wal
# sitting next to a restored file is a corruption hazard, not a spare copy.
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


class BackupError(RuntimeError):
    """Anything that means "this run did not produce a good backup"."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def archive_name(when: datetime = None) -> str:
    when = when or _now()
    return f"{ARCHIVE_PREFIX}{when:%Y%m%dT%H%M%SZ}{ARCHIVE_SUFFIX}"


def is_archive(name: str) -> bool:
    return name.startswith(ARCHIVE_PREFIX) and name.endswith(ARCHIVE_SUFFIX)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -- destinations ------------------------------------------------------------


class LocalDestination:
    """A directory. A mounted volume, an sshfs mount, a NAS -- or tmp_path.

    Worth saying out loud: a directory on the *same disk as the data* is not a
    backup, it is a second copy of a file that will be lost with the first. The
    CLI warns when it detects that; it does not refuse, because a local
    directory that is a mount point is exactly the sensible cheap answer.
    """

    scheme = "local"

    def __init__(self, root):
        self.root = Path(root).expanduser()

    def __repr__(self):
        return f"<LocalDestination {self.root}>"

    def describe(self) -> str:
        return str(self.root)

    def put(self, path: Path, name: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # Write to a temporary neighbour and rename, so an interrupted copy
        # never appears in the listing as a complete backup.
        tmp = self.root / (name + ".part")
        shutil.copyfile(path, tmp)
        os.replace(tmp, self.root / name)

    def list(self) -> list:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if is_archive(p.name))

    def get(self, name: str, into: Path) -> Path:
        src = self.root / name
        if not src.is_file():
            raise BackupError(f"no such backup: {name}")
        into.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, into)
        return into

    def delete(self, name: str) -> None:
        (self.root / name).unlink(missing_ok=True)


class S3Destination:
    """Any S3-compatible object store, via boto3.

    boto3 is an optional dependency: it is only needed once there is a bucket,
    and importing it here rather than at module scope keeps the whole backup
    module -- and its tests -- runnable without it.

    Credentials come from the standard AWS environment variables, so this
    module holds no secrets and nothing new has to be taught how to read them.
    A non-Amazon provider needs `BACKUP_S3_ENDPOINT` (or `AWS_ENDPOINT_URL`).
    """

    scheme = "s3"

    def __init__(self, bucket, prefix="", endpoint=None, region=None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.endpoint = endpoint or os.environ.get("BACKUP_S3_ENDPOINT") \
            or os.environ.get("AWS_ENDPOINT_URL") or None
        self.region = region or os.environ.get("AWS_REGION") \
            or os.environ.get("AWS_DEFAULT_REGION") or None
        self._client = None

    def __repr__(self):
        return f"<S3Destination s3://{self.bucket}/{self.prefix}>"

    def describe(self) -> str:
        where = f"s3://{self.bucket}/{self.prefix}".rstrip("/")
        return f"{where} (endpoint {self.endpoint})" if self.endpoint else where

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3  # noqa: F401
            except ImportError as exc:  # pragma: no cover - depends on env
                raise BackupError(
                    "the s3 destination needs boto3: pip install boto3"
                ) from exc
            kwargs = {}
            if self.endpoint:
                kwargs["endpoint_url"] = self.endpoint
            if self.region:
                kwargs["region_name"] = self.region
            self._client = boto3.client("s3", **kwargs)
        return self._client

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def put(self, path: Path, name: str) -> None:
        self.client.upload_file(str(path), self.bucket, self._key(name))

    def list(self) -> list:
        paginator = self.client.get_paginator("list_objects_v2")
        prefix = self._key(ARCHIVE_PREFIX)
        names = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"].rsplit("/", 1)[-1]
                if is_archive(name):
                    names.append(name)
        return sorted(names)

    def get(self, name: str, into: Path) -> Path:
        into.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, self._key(name), str(into))
        return into

    def delete(self, name: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(name))


def destination_for(spec: str):
    """Turn a `BACKUP_DEST` string into a destination object.

    Accepts `s3://bucket/prefix`, `file:///var/backups`, or a bare path. A
    scheme this does not know is an error rather than a fallback to local: a
    typo'd `s4://` silently writing to a directory called `s4:` is the kind of
    thing that is discovered on the day the disk dies.
    """
    spec = (spec or "").strip()
    if not spec:
        raise BackupError(
            "no backup destination: set BACKUP_DEST in .env or pass --dest"
        )
    if spec.startswith("s3://"):
        rest = spec[len("s3://"):]
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise BackupError(f"malformed destination: {spec}")
        return S3Destination(bucket, prefix)
    if spec.startswith("file://"):
        return LocalDestination(spec[len("file://"):])
    if "://" in spec:
        scheme = spec.split("://", 1)[0]
        raise BackupError(
            f"unknown backup destination scheme {scheme!r}; "
            "use s3://bucket/prefix, file:///path, or a plain path"
        )
    return LocalDestination(spec)


# -- making one --------------------------------------------------------------


def snapshot_db(db_path: Path, out_path: Path) -> None:
    """Copy a live SQLite database consistently, WAL and all.

    `sqlite3.Connection.backup` is the whole reason this function exists; see
    the module docstring. The source is opened read-only in its own connection
    because this runs in a separate process from the server, and it must not
    inherit or disturb the server's connections.
    """
    if not db_path.is_file():
        raise BackupError(f"no database at {db_path}")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(out_path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def build_archive(data_dir: Path, out_path: Path, db_path: Path = None) -> dict:
    """Write one .tar.gz of everything, and return its manifest.

    Layout inside the archive:

        MANIFEST.json   what this is, when, and how many of what
        app.db          a consistent snapshot, safe to open directly
        tables/<id>/... every table's prose, exactly as it sits on disk
    """
    data_dir = Path(data_dir)
    db_path = Path(db_path) if db_path else data_dir / "app.db"
    tables_dir = data_dir / "tables"

    with tempfile.TemporaryDirectory(prefix="familiar-backup-") as tmp:
        tmp = Path(tmp)
        staged_db = tmp / "app.db"

        # Files first, then the database -- see the module docstring.
        members = []
        file_bytes = 0
        if tables_dir.is_dir():
            for path in sorted(tables_dir.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    members.append((path, "tables/" + path.relative_to(tables_dir).as_posix()))
                    file_bytes += path.stat().st_size

        snapshot_db(db_path, staged_db)

        table_ids = sorted({m[1].split("/")[1] for m in members if "/" in m[1][7:]})
        manifest = {
            "format": FORMAT_VERSION,
            "created_at": _now().isoformat(timespec="seconds"),
            "host": os.uname().nodename,
            "data_dir": str(data_dir),
            "table_dirs": len(table_ids),
            "files": len(members),
            "file_bytes": file_bytes,
            "db_bytes": staged_db.stat().st_size,
        }
        manifest_path = tmp / "MANIFEST.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out_path, "w:gz") as tar:
            tar.add(manifest_path, arcname="MANIFEST.json")
            tar.add(staged_db, arcname="app.db")
            for path, arcname in members:
                tar.add(path, arcname=arcname)

    manifest["archive_bytes"] = out_path.stat().st_size
    manifest["sha256"] = sha256_of(out_path)
    return manifest


# -- checking one ------------------------------------------------------------


def _safe_extract(tar: tarfile.TarFile, member_name: str, into: Path) -> Path:
    """Pull one named member out, refusing anything that escapes `into`.

    A backup archive is written by this module and read by an operator with
    root, which is exactly the combination that makes path traversal worth
    guarding even when the file is nominally ours -- it may have come back
    from a bucket somebody else can write to.
    """
    member = tar.getmember(member_name)
    if not member.isfile():
        raise BackupError(f"{member_name} is not a regular file")
    target = (into / member_name).resolve()
    if not str(target).startswith(str(into.resolve()) + os.sep):
        raise BackupError(f"archive member escapes the target directory: {member_name}")
    with tar.extractfile(member) as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return target


def verify_archive(path: Path) -> dict:
    """Reopen a backup and prove it would restore. Raises BackupError if not.

    Three questions, in order of how badly the answer matters:

    1. Is the database in there a database? `PRAGMA integrity_check` on the
       extracted copy. A truncated or half-copied file fails here.
    2. Does it hold the things the service needs? The `tables` and `users`
       rows are read back and counted.
    3. Does every table row have its files? A row without a directory is
       reported as a warning rather than a failure, because a table created
       during the run legitimately looks like this (files are captured first)
       and the next night's run will include it.
    """
    path = Path(path)
    report = {"archive": path.name, "warnings": []}
    with tempfile.TemporaryDirectory(prefix="familiar-verify-") as tmp:
        tmp = Path(tmp)
        try:
            tar = tarfile.open(path, "r:gz")
        except (tarfile.TarError, OSError) as exc:
            raise BackupError(f"{path.name} is not a readable archive: {exc}") from exc
        with tar:
            names = set(tar.getnames())
            for required in ("MANIFEST.json", "app.db"):
                if required not in names:
                    raise BackupError(f"{path.name} has no {required}")
            manifest = json.loads(_safe_extract(tar, "MANIFEST.json", tmp).read_text())
            if manifest.get("format") != FORMAT_VERSION:
                raise BackupError(
                    f"{path.name} is format {manifest.get('format')}, "
                    f"this is version {FORMAT_VERSION}"
                )
            db_copy = _safe_extract(tar, "app.db", tmp)
            dirs_in_archive = {
                n.split("/")[1] for n in names
                if n.startswith("tables/") and n.count("/") >= 2
            }

        con = sqlite3.connect(db_copy)
        try:
            # A badly damaged file makes the check itself raise rather than
            # return a verdict, so both paths lead to the same BackupError --
            # the caller only ever needs to know "this would not restore".
            try:
                ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise BackupError(
                    f"{path.name}: database fails integrity check ({exc})") from exc
            if ok != "ok":
                raise BackupError(f"{path.name}: database fails integrity check ({ok})")
            try:
                rows = con.execute("SELECT id, slug FROM tables").fetchall()
                users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise BackupError(f"{path.name}: database is unreadable ({exc})") from exc
        finally:
            con.close()

    report["manifest"] = manifest
    report["tables"] = len(rows)
    report["users"] = users
    for table_id, slug in rows:
        if table_id not in dirs_in_archive:
            report["warnings"].append(
                f"table {slug} ({table_id}) has a row but no files in the archive"
            )
    for table_id in sorted(dirs_in_archive - {r[0] for r in rows}):
        report["warnings"].append(
            f"directory tables/{table_id} has files but no row (a deleted table?)"
        )
    return report


# -- keeping a few of them ---------------------------------------------------


def prune(dest, keep: int = DEFAULT_KEEP) -> list:
    """Delete all but the newest `keep` archives. Returns what it deleted.

    Names sort chronologically because they are ISO-ish UTC timestamps, which
    is the entire reason for that naming. `keep=0` means keep everything: an
    operator who wants nothing kept can empty the bucket themselves, and a
    misread config should never be the thing that deletes every backup.
    """
    if keep <= 0:
        return []
    names = dest.list()
    doomed = names[:-keep] if len(names) > keep else []
    for name in doomed:
        dest.delete(name)
    return doomed


def run_backup(data_dir, dest, keep: int = DEFAULT_KEEP, db_path=None) -> dict:
    """One whole nightly run: build, verify, upload, prune.

    Verification happens on the local file *before* the upload, so a bad
    archive is never stored at all -- and never prunes a good one out of the
    retention window by taking its slot.
    """
    data_dir = Path(data_dir)
    name = archive_name()
    with tempfile.TemporaryDirectory(prefix="familiar-run-") as tmp:
        local = Path(tmp) / name
        manifest = build_archive(data_dir, local, db_path=db_path)
        check = verify_archive(local)
        dest.put(local, name)
    report = {
        "ok": True,
        "archive": name,
        "destination": dest.describe(),
        "bytes": manifest["archive_bytes"],
        "sha256": manifest["sha256"],
        "tables": check["tables"],
        "users": check["users"],
        "files": manifest["files"],
        "warnings": check["warnings"],
        "finished_at": _now().isoformat(timespec="seconds"),
    }
    report["pruned"] = prune(dest, keep)
    return report


def write_status(data_dir, report: dict) -> Path:
    """Leave the last run's report beside the data, for a human or a check.

    A backup system's characteristic failure is not a loud one; it is silence
    for four months. This file is the cheapest thing that can be looked at --
    `cat data/last-backup.json` -- and it is written for failures too.
    """
    path = Path(data_dir) / "last-backup.json"
    try:
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return path


def restore(dest, name: str, into: Path) -> dict:
    """Unpack one backup into an empty directory. Never over the live one.

    `into` must not exist or must be empty. Restoring in place, over a running
    service, with a half-verified archive, at 3am, is how a bad night becomes
    a catastrophic one -- so this refuses, and the operator moves the restored
    directory into place themselves once they have looked at it.
    """
    into = Path(into)
    if into.exists() and any(into.iterdir()):
        raise BackupError(f"{into} is not empty; restore into a new directory")
    into.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="familiar-restore-") as tmp:
        local = dest.get(name, Path(tmp) / name)
        check = verify_archive(local)
        with tarfile.open(local, "r:gz") as tar:
            root = into.resolve()
            for member in tar.getmembers():
                target = (into / member.name).resolve()
                if not str(target).startswith(str(root) + os.sep):
                    raise BackupError(f"archive member escapes the target: {member.name}")
                if member.issym() or member.islnk():
                    raise BackupError(f"archive contains a link: {member.name}")
            # `filter="data"` is the same guard again from the stdlib's side
            # (no absolute paths, no links, no device nodes). Both, because the
            # loop above gives a message naming the offending member and the
            # filter catches the cases nobody thought of.
            tar.extractall(into, filter="data")
    for sidecar in SQLITE_SIDECARS:
        (into / f"app.db{sidecar}").unlink(missing_ok=True)
    return {"restored": name, "into": str(into), "tables": check["tables"],
            "warnings": check["warnings"]}
