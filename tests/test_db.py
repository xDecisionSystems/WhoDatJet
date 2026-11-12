from __future__ import annotations

import sqlite3

import pytest

from whodatjet import db
from whodatjet.cli import main


def write(d, name, sql):
    (d / name).write_text(sql)


def test_packaged_migrations_load_for_both_databases():
    for name in db.DATABASES:
        migs = db.load_migrations(name)
        assert migs and migs[0].version == 1


def test_writer_uses_wal_and_foreign_keys(tmp_path):
    conn = db.connect_writer(tmp_path / "x.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS


def test_reader_cannot_write(tmp_path):
    path = tmp_path / "x.db"
    w = db.connect_writer(path)
    w.execute("CREATE TABLE t (x)")
    r = db.connect_reader(path)
    assert r.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        r.execute("INSERT INTO t VALUES (1)")


def test_reader_sees_only_committed_data(tmp_path):
    path = tmp_path / "x.db"
    w = db.connect_writer(path)
    w.execute("CREATE TABLE t (x)")
    r = db.connect_reader(path)
    w.execute("BEGIN")
    w.execute("INSERT INTO t VALUES (1)")
    assert r.execute("SELECT count(*) FROM t").fetchone()[0] == 0  # snapshot isolation under WAL
    w.execute("COMMIT")
    assert r.execute("SELECT count(*) FROM t").fetchone()[0] == 1


def test_migrations_apply_in_order_and_are_idempotent(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    write(d, "0002_second.sql", "ALTER TABLE a ADD COLUMN y;")
    write(d, "0001_first.sql", "CREATE TABLE a (x);")
    conn = db.connect_writer(tmp_path / "x.db")
    migs = db.load_migrations("registry", d)
    assert [m.version for m in db.migrate(conn, migs)] == [1, 2]
    assert db.migrate(conn, migs) == []
    assert [r[1] for r in conn.execute("PRAGMA table_info(a)")] == ["x", "y"]


def test_failed_migration_rolls_back_entirely(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    write(d, "0001_first.sql", "CREATE TABLE a (x);")
    write(d, "0002_bad.sql", "CREATE TABLE b (x); INSERT INTO nope VALUES (1);")
    conn = db.connect_writer(tmp_path / "x.db")
    with pytest.raises(db.MigrationError, match="0002_bad"):
        db.migrate(conn, db.load_migrations("registry", d))
    assert set(db.applied_versions(conn)) == {1}
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='b'").fetchone() is None


def test_modified_migration_is_refused(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    write(d, "0001_first.sql", "CREATE TABLE a (x);")
    conn = db.connect_writer(tmp_path / "x.db")
    db.migrate(conn, db.load_migrations("registry", d))
    write(d, "0001_first.sql", "CREATE TABLE a (x, z);")
    with pytest.raises(db.MigrationError, match="modified"):
        db.migrate(conn, db.load_migrations("registry", d))


def test_badly_named_migration_rejected(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    write(d, "1_first.sql", "SELECT 1;")
    with pytest.raises(db.MigrationError, match="badly named"):
        db.load_migrations("registry", d)


def test_cli_migrate_without_s3_settings(tmp_path, monkeypatch, capsys):
    for k in ("WHODATJET_S3_BUCKET", "WHODATJET_S3_ACCESS_KEY_ID", "WHODATJET_S3_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WHODATJET_DATA_DIR", str(tmp_path))
    assert main(["migrate"]) == 0
    assert (tmp_path / "whodatjet.db").exists() and (tmp_path / "admin.db").exists()
    assert main(["migrate", "--status"]) == 0
    assert "applied" in capsys.readouterr().out
