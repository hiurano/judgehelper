"""Create a consistent online SQLite backup before deployment."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from scripts.preflight import ROOT, load_env


def resolve_host_path(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def create_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)


def main() -> int:
    env = load_env(ROOT / ".env")
    configured = Path(env.get("DB_PATH", "backend/data/jobs.db"))
    data_dir = resolve_host_path(env.get("HOST_DATA_DIR", "backend/data"))
    source = data_dir / configured.name
    if not source.exists():
        print("Database does not exist yet; backup skipped.")
        return 0

    backup_dir = resolve_host_path(env.get("BACKUP_DIR", "backend/data/backups"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = backup_dir / f"jobs-{stamp}.db"
    create_backup(source, destination)

    print(f"Database backup created: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
