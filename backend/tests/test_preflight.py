import sqlite3
from pathlib import Path

import pytest

from scripts.backup_db import create_backup, prune_backups
from scripts.preflight import load_env, main


def test_load_env_parses_comments_and_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nPLAIN=value\nQUOTED='secret value'\n",
        encoding="utf-8",
    )
    assert load_env(env_file) == {
        "PLAIN": "value",
        "QUOTED": "secret value",
    }


def test_load_env_rejects_malformed_lines(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("BROKEN_LINE\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_env(env_file)


def test_create_backup_copies_consistent_sqlite_database(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "backups" / "copy.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO jobs (name) VALUES (?)", ("hearing",))

    create_backup(source, destination)

    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT name FROM jobs").fetchall() == [("hearing",)]


def test_prune_backups_only_removes_expired_managed_files(tmp_path):
    old_backup = tmp_path / "jobs-old.db"
    recent_backup = tmp_path / "jobs-recent.db"
    unrelated = tmp_path / "predeploy-keep.db"
    for path in (old_backup, recent_backup, unrelated):
        path.write_bytes(b"sqlite")
    old_backup.touch()
    recent_backup.touch()
    unrelated.touch()
    import os
    os.utime(old_backup, (100, 100))

    removed = prune_backups(tmp_path, 1, now=100 + 2 * 86400)

    assert removed == 1
    assert not old_backup.exists()
    assert recent_backup.exists()
    assert unrelated.exists()


def test_preflight_reports_database_sidecar_owned_by_another_user(tmp_path, capsys):
    """A -wal left behind by a backup run as another user locks the app out."""
    import os

    data_dir = tmp_path / "data"
    logs_dir = tmp_path / "logs"
    data_dir.mkdir()
    logs_dir.mkdir()
    (data_dir / "jobs.db").touch()
    (data_dir / "jobs.db-wal").touch()

    env_file = tmp_path / ".env"
    env_file.write_text(
        "DB_PATH=backend/data/jobs.db\n"
        f"HOST_DATA_DIR={data_dir}\n"
        f"HOST_LOGS_DIR={logs_dir}\n"
        # Anything but the uid owning the files above.
        f"APP_UID={os.getuid() + 1}\n"
        f"APP_GID={os.getgid() + 1}\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    assert main(env_file) == 1
    assert "database WAL file owner" in capsys.readouterr().err
