import sqlite3
from pathlib import Path

import pytest

from scripts.backup_db import create_backup
from scripts.preflight import load_env


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
