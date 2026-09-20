"""Change values in the production env file without disturbing the file itself.

Run through scripts/set-config.sh, which supplies the privileges needed to
write a root-owned file. Uses only the standard library.
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_assignments(arguments: list[str]) -> dict[str, str]:
    updates: dict[str, str] = {}
    for argument in arguments:
        if "=" not in argument:
            raise ValueError(f"expected KEY=VALUE, got {argument!r}")
        key, value = argument.split("=", 1)
        key = key.strip()
        if not key or not all(c.isalnum() or c == "_" for c in key):
            raise ValueError(f"not a usable key: {key!r}")
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key} value must be a single line")
        updates[key] = value
    if not updates:
        raise ValueError("nothing to do: pass at least one KEY=VALUE")
    return updates


def apply_updates(lines: list[str], updates: dict[str, str]) -> tuple[list[str], list[str]]:
    """Rewrite assignments in place, keeping comments and order. Appends new keys."""
    remaining = dict(updates)
    changed: list[str] = []
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                value = remaining.pop(key)
                if stripped != f"{key}={value}":
                    changed.append(key)
                result.append(f"{key}={value}")
                continue
        result.append(line)

    for key, value in remaining.items():
        result.append(f"{key}={value}")
        changed.append(key)

    return result, changed


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: set_config.py <env-file> KEY=VALUE [KEY=VALUE ...]", file=sys.stderr)
        return 2

    env_path = Path(argv[1])
    try:
        updates = parse_assignments(argv[2:])
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not env_path.is_file():
        print(f"ERROR: no such file: {env_path}", file=sys.stderr)
        return 1

    lines = env_path.read_text(encoding="utf-8").splitlines()
    updated, changed = apply_updates(lines, updates)

    if not changed:
        print("Nothing to change; every value already matches.")
        return 0

    # Timestamped backup with the original owner and mode: it holds secrets.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = env_path.with_name(f"{env_path.name}.{stamp}.bak")
    shutil.copy2(env_path, backup)
    source = env_path.stat()
    os.chown(backup, source.st_uid, source.st_gid)

    # Truncate and rewrite rather than replace: keeps the inode, owner and mode,
    # which matters because the app reads this file through a symlink.
    with env_path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(updated) + "\n")

    # Names only. The values are secrets and must not reach a terminal or log.
    print(f"Updated: {', '.join(sorted(changed))}")
    print(f"Backup:  {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
