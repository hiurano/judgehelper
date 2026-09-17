"""Fail-fast production configuration check. Uses only the Python standard library."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        raise ValueError(f"missing {path}")
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def main() -> int:
    env_path = ROOT / ".env"
    try:
        env = load_env(env_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    errors: list[str] = []
    placeholders = ("your_", "example.com", "your-domain")

    if env_path.stat().st_mode & 0o077:
        errors.append(".env must not be readable or writable by group/others (use chmod 600 .env)")

    for key in ("ASSEMBLYAI_API_KEY", "OPENROUTER_API_KEY", "AUTH_USERNAME"):
        value = env.get(key, "")
        if not value or any(marker in value for marker in placeholders):
            errors.append(f"{key} is missing or still contains a placeholder")

    password = env.get("AUTH_PASSWORD", "")
    if (
        len(password) < 12
        or password == env.get("AUTH_USERNAME")
        or any(marker in password for marker in placeholders)
    ):
        errors.append("AUTH_PASSWORD must be at least 12 characters and differ from AUTH_USERNAME")

    for key in ("SECRET_KEY", "WEBHOOK_SECRET"):
        value = env.get(key, "")
        if len(value) < 32 or any(marker in value for marker in placeholders):
            errors.append(f"{key} must be a non-placeholder secret of at least 32 characters")

    domain = env.get("CADDY_DOMAIN", "")
    if not domain or "://" in domain or any(marker in domain for marker in placeholders):
        errors.append("CADDY_DOMAIN must be a hostname without a URL scheme")

    base_url = env.get("BASE_URL", "")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or parsed.netloc != domain:
        errors.append("BASE_URL must be https:// followed by CADDY_DOMAIN")

    if "*" in {item.strip() for item in env.get("ALLOWED_ORIGINS", "").split(",")}:
        errors.append("ALLOWED_ORIGINS must not contain * in production")

    db_path = Path(env.get("DB_PATH", "backend/data/jobs.db"))
    resolved_db = (ROOT / db_path).resolve() if not db_path.is_absolute() else db_path.resolve()
    data_root = (ROOT / "backend/data").resolve()
    if resolved_db != data_root / "jobs.db" and data_root not in resolved_db.parents:
        errors.append("DB_PATH must point inside backend/data so Docker persists it")

    prompt = ROOT / "prompts" / "system-protocol.md"
    if not prompt.is_file() or not prompt.read_text(encoding="utf-8").strip():
        errors.append("prompts/system-protocol.md is missing or empty")

    try:
        app_uid = int(env.get("APP_UID", "1000"))
        app_gid = int(env.get("APP_GID", "1000"))
    except ValueError:
        errors.append("APP_UID and APP_GID must be integers")
        app_uid = app_gid = -1

    for relative in ("backend/data", "backend/logs"):
        directory = ROOT / relative
        directory.mkdir(parents=True, exist_ok=True)
        if not os.access(directory, os.W_OK):
            errors.append(f"{relative} is not writable by the current user")
        stat = directory.stat()
        if stat.st_uid != app_uid or stat.st_gid != app_gid:
            errors.append(
                f"{relative} owner is {stat.st_uid}:{stat.st_gid}, expected APP_UID:APP_GID "
                f"{app_uid}:{app_gid}"
            )

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Production preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
