"""Create a consistent online SQLite backup before deployment."""
from __future__ import annotations

import sqlite3
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.preflight import ROOT, load_env


def resolve_host_path(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def create_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)


def prune_backups(backup_dir: Path, retention_days: int, *, now: float | None = None) -> int:
    if retention_days < 1:
        raise ValueError("BACKUP_RETENTION_DAYS must be at least 1")
    cutoff = (now if now is not None else time.time()) - retention_days * 86400
    removed = 0
    for backup in backup_dir.glob("jobs-*.db"):
        if backup.is_file() and backup.stat().st_mtime < cutoff:
            backup.unlink()
            removed += 1
    return removed


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
    os.chown(destination, -1, backup_dir.stat().st_gid)
    destination.chmod(0o660)

    try:
        retention_days = int(env.get("BACKUP_RETENTION_DAYS", "30"))
        removed = prune_backups(backup_dir, retention_days)
    except ValueError as exc:
        raise ValueError("BACKUP_RETENTION_DAYS must be an integer of at least 1") from exc

    print(f"Database backup created: {destination}")
    if removed:
        print(f"Removed {removed} expired database backup(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
