"""The backup system: does it capture everything, and would it restore?

The tests worth reading here are `test_snapshot_captures_unflushed_wal_writes`
(the reason a backup cannot be a `cp`) and the two that make a bad archive
fail: a backup nobody has ever checked is a belief, not a copy.
"""

import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

import backup
import db as database


@pytest.fixture
def service(tmp_path):
    """A data directory shaped like a real one: a database with a user and two
    tables, and each table's prose on disk beside it."""
    data = tmp_path / "data"
    handle = database.Database(data / "app.db")
    user = handle.create_user("dm@example.com", "a-long-enough-password")
    ids = []
    for slug in ("grove", "second"):
        table_id = handle.create_table(user, slug.title(), slug)
        ids.append(table_id)
        d = data / "tables" / table_id
        (d / "sessions").mkdir(parents=True)
        (d / "campaign.md").write_text(f"# {slug}\nA campaign.\n", encoding="utf-8")
        (d / "cast.json").write_text(json.dumps({"npcs": [{"name": "Seraphine"}]}), encoding="utf-8")
        (d / "sessions" / "2026-08-21.log").write_text('{"q":"who?","a":"me"}\n',
                                                       encoding="utf-8")

    class Service:
        pass

    s = Service()
    s.data = data
    s.db = handle
    s.user = user
    s.table_ids = ids
    return s


@pytest.fixture
def dest(tmp_path):
    return backup.LocalDestination(tmp_path / "offsite")


# -- the reason this module exists ------------------------------------------


def test_snapshot_captures_unflushed_wal_writes(service, tmp_path):
    """A file copy of app.db loses recent writes; the snapshot does not.

    This is the whole argument for `snapshot_db`. The database runs in WAL
    mode, so a write lands in app.db-wal and may sit there for a long time.
    Copying app.db alone gives a file that opens cleanly and is missing the
    evening.
    """
    con = service.db.connect()          # WAL, and deliberately not checkpointed
    service.db.create_table(service.user, "Late", "late-table")
    assert con.execute("SELECT COUNT(*) FROM tables").fetchone()[0] == 3

    naive = tmp_path / "naive.db"
    naive.write_bytes((service.data / "app.db").read_bytes())
    good = tmp_path / "good.db"
    backup.snapshot_db(service.data / "app.db", good)

    naive_con = sqlite3.connect(naive)
    good_con = sqlite3.connect(good)
    try:
        assert good_con.execute("SELECT COUNT(*) FROM tables").fetchone()[0] == 3
        try:
            naive_count = naive_con.execute("SELECT COUNT(*) FROM tables").fetchone()[0]
        except sqlite3.DatabaseError:
            # On a young database it is worse than stale: the schema itself is
            # still in the WAL, so the copy has no `tables` table at all.
            naive_count = 0
    finally:
        naive_con.close()
        good_con.close()
    assert naive_count < 3


# -- a whole run -------------------------------------------------------------


def test_run_backup_stores_verifies_and_reports(service, dest):
    report = backup.run_backup(service.data, dest)

    assert report["ok"] is True
    assert report["tables"] == 2
    assert report["users"] == 1
    assert report["warnings"] == []
    assert dest.list() == [report["archive"]]
    assert backup.sha256_of(dest.root / report["archive"]) == report["sha256"]


def test_archive_holds_the_files_and_a_usable_database(service, dest):
    report = backup.run_backup(service.data, dest)
    with tarfile.open(dest.root / report["archive"], "r:gz") as tar:
        names = set(tar.getnames())

    assert "app.db" in names and "MANIFEST.json" in names
    for table_id in service.table_ids:
        assert f"tables/{table_id}/campaign.md" in names
        assert f"tables/{table_id}/sessions/2026-08-21.log" in names
    # sqlite's sidecars are never archived: a stale -wal beside a restored
    # snapshot is a corruption hazard, not a spare copy.
    assert not [n for n in names if n.endswith(("-wal", "-shm"))]


def test_restore_round_trips_every_byte(service, dest, tmp_path):
    report = backup.run_backup(service.data, dest)
    into = tmp_path / "restored"
    result = backup.restore(dest, report["archive"], into)

    assert result["tables"] == 2
    for table_id in service.table_ids:
        original = service.data / "tables" / table_id
        for path in original.rglob("*"):
            if path.is_file():
                copy = into / "tables" / table_id / path.relative_to(original)
                assert copy.read_bytes() == path.read_bytes()

    con = sqlite3.connect(into / "app.db")
    try:
        slugs = {r[0] for r in con.execute("SELECT slug FROM tables")}
    finally:
        con.close()
    assert slugs == {"grove", "second"}
    assert not (into / "app.db-wal").exists()


def test_a_restored_database_is_a_working_one(service, dest, tmp_path):
    """Restoring gives back a Database the app can open, not just a file."""
    report = backup.run_backup(service.data, dest)
    into = tmp_path / "restored"
    backup.restore(dest, report["archive"], into)

    restored = database.Database(into / "app.db")
    row = restored.table_by_slug("grove")
    assert row is not None and row["owner_id"] == service.user
    assert len(restored.tables_for_user(service.user)) == 2


def test_status_file_records_the_last_run(service, dest):
    report = backup.run_backup(service.data, dest)
    path = backup.write_status(service.data, report)
    assert json.loads(path.read_text())["archive"] == report["archive"]


# -- refusing to call a bad archive good -------------------------------------


def test_verify_rejects_a_corrupted_database(service, dest, tmp_path):
    """Scribble over the database inside an archive and rebuild it: verify must
    notice. This is the failure mode that otherwise stays invisible for months."""
    report = backup.run_backup(service.data, dest)
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with tarfile.open(dest.root / report["archive"], "r:gz") as tar:
        tar.extractall(unpacked, filter="data")
    with open(unpacked / "app.db", "r+b") as f:
        f.seek(4096)
        f.write(b"\xff" * 8192)
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        for path in sorted(unpacked.rglob("*")):
            tar.add(path, arcname=str(path.relative_to(unpacked)))

    with pytest.raises(backup.BackupError, match="integrity"):
        backup.verify_archive(bad)


def test_verify_rejects_something_that_is_not_an_archive(tmp_path):
    junk = tmp_path / "familiar-20260101T000000Z.tar.gz"
    junk.write_bytes(b"not a tarball, just some bytes")
    with pytest.raises(backup.BackupError, match="readable archive"):
        backup.verify_archive(junk)


def test_verify_rejects_an_archive_with_no_database(tmp_path):
    path = tmp_path / "empty.tar.gz"
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps({"format": backup.FORMAT_VERSION}))
    with tarfile.open(path, "w:gz") as tar:
        tar.add(manifest, arcname="MANIFEST.json")
    with pytest.raises(backup.BackupError, match="no app.db"):
        backup.verify_archive(path)


def test_verify_rejects_a_future_format(service, dest, tmp_path, monkeypatch):
    report = backup.run_backup(service.data, dest)
    monkeypatch.setattr(backup, "FORMAT_VERSION", backup.FORMAT_VERSION + 1)
    with pytest.raises(backup.BackupError, match="format"):
        backup.verify_archive(dest.root / report["archive"])


def test_verify_warns_about_a_table_with_no_files(service, dest, tmp_path):
    """A row whose directory is missing is a warning, not a failure: a table
    created while the run was walking the filesystem legitimately looks like
    this, and tomorrow's run will have it."""
    import shutil
    shutil.rmtree(service.data / "tables" / service.table_ids[0])
    report = backup.run_backup(service.data, dest)
    assert report["ok"] is True
    assert any("no files" in w for w in report["warnings"])


def test_restore_refuses_a_directory_with_anything_in_it(service, dest, tmp_path):
    report = backup.run_backup(service.data, dest)
    occupied = tmp_path / "live"
    occupied.mkdir()
    (occupied / "app.db").write_text("the running service", encoding="utf-8")
    with pytest.raises(backup.BackupError, match="not empty"):
        backup.restore(dest, report["archive"], occupied)
    assert (occupied / "app.db").read_text() == "the running service"


def test_restore_refuses_an_archive_that_escapes_its_directory(tmp_path, dest):
    """Backups come back from storage somebody else can write to. An archive
    member called ../../etc/anything must not be written."""
    payload = tmp_path / "payload"
    payload.write_text("owned", encoding="utf-8")
    name = backup.archive_name()
    evil = tmp_path / name
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(payload, arcname="../escaped.txt")
    dest.put(evil, name)

    with pytest.raises(backup.BackupError):
        backup.restore(dest, name, tmp_path / "into")
    assert not (tmp_path / "escaped.txt").exists()


# -- retention ---------------------------------------------------------------


def _store(dest, count):
    names = []
    for i in range(count):
        name = f"{backup.ARCHIVE_PREFIX}202608{10 + i:02d}T030000Z{backup.ARCHIVE_SUFFIX}"
        (dest.root).mkdir(parents=True, exist_ok=True)
        (dest.root / name).write_bytes(b"x")
        names.append(name)
    return names


def test_prune_keeps_the_newest(dest):
    names = _store(dest, 10)
    deleted = backup.prune(dest, keep=3)
    assert deleted == names[:7]
    assert dest.list() == names[7:]


def test_prune_keeps_everything_when_keep_is_zero(dest):
    names = _store(dest, 5)
    assert backup.prune(dest, keep=0) == []
    assert dest.list() == names


def test_prune_ignores_files_that_are_not_backups(dest):
    names = _store(dest, 3)
    (dest.root / "notes.txt").write_text("not a backup", encoding="utf-8")
    backup.prune(dest, keep=1)
    assert (dest.root / "notes.txt").exists()
    assert dest.list() == names[-1:]


def test_a_run_prunes_as_it_goes(service, dest):
    _store(dest, 5)
    report = backup.run_backup(service.data, dest, keep=2)
    assert len(dest.list()) == 2
    assert report["archive"] in dest.list()


# -- destinations ------------------------------------------------------------


def test_destination_for_reads_the_shapes_an_operator_types(tmp_path):
    assert isinstance(backup.destination_for("/mnt/backups"), backup.LocalDestination)
    assert backup.destination_for("file:///mnt/b").root == Path("/mnt/b")
    s3 = backup.destination_for("s3://bucket/familiar/nightly")
    assert (s3.bucket, s3.prefix) == ("bucket", "familiar/nightly")
    assert backup.destination_for("s3://bucket").prefix == ""


def test_destination_for_refuses_a_scheme_it_does_not_know():
    """A typo'd scheme must not quietly become a directory called 's4:'."""
    with pytest.raises(backup.BackupError, match="unknown"):
        backup.destination_for("s4://bucket/prefix")


def test_destination_for_refuses_nothing_at_all():
    with pytest.raises(backup.BackupError, match="BACKUP_DEST"):
        backup.destination_for("")


def test_local_put_is_atomic_in_the_listing(service, dest):
    """A half-written copy must never appear as a finished backup."""
    report = backup.run_backup(service.data, dest)
    assert dest.list() == [report["archive"]]
    assert not [p for p in dest.root.iterdir() if p.name.endswith(".part")]


def test_archive_names_sort_chronologically():
    """Retention depends on it -- prune sorts by name, not by mtime."""
    from datetime import datetime, timezone
    early = backup.archive_name(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    late = backup.archive_name(datetime(2026, 12, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert early < late
    assert backup.is_archive(early) and not backup.is_archive("app.db")
