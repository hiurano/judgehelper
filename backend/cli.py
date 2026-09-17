"""
CLI for managing Judge Helper user accounts.

Usage:
    python -m backend.cli add-user <username> [password] [--display-name NAME]
    python -m backend.cli list-users
    python -m backend.cli change-password <username> [new_password]
    python -m backend.cli delete-user <username>
"""
import argparse
import getpass
import sys

from backend.db import user_store


def _require_strong_password(password: str) -> None:
    if len(password) < 12:
        raise SystemExit("Password must contain at least 12 characters.")


def main():
    parser = argparse.ArgumentParser(description="Judge Helper — User Management CLI")
    sub = parser.add_subparsers(dest="command")

    # add-user
    add_p = sub.add_parser("add-user", help="Create a new user account")
    add_p.add_argument("username")
    add_p.add_argument("password", nargs="?", help="Omit to enter it securely at the prompt")
    add_p.add_argument("--display-name", default="", help="Display name (defaults to capitalized username)")

    # list-users
    sub.add_parser("list-users", help="List all user accounts")

    # change-password
    chg_p = sub.add_parser("change-password", help="Change a user's password")
    chg_p.add_argument("username")
    chg_p.add_argument("new_password", nargs="?", help="Omit to enter it securely at the prompt")

    # delete-user
    del_p = sub.add_parser("delete-user", help="Delete a user account")
    del_p.add_argument("username")

    args = parser.parse_args()

    if args.command == "add-user":
        password = args.password or getpass.getpass("Password: ")
        _require_strong_password(password)
        ok = user_store.create_user(args.username, password, args.display_name)
        if ok:
            print(f"✓ User '{args.username}' created.")
        else:
            print(f"✗ User '{args.username}' already exists.", file=sys.stderr)
            sys.exit(1)

    elif args.command == "list-users":
        users = user_store.list_users()
        if not users:
            print("No users found.")
        else:
            print(f"{'Username':<20} {'Display Name':<20} {'Created'}")
            print("-" * 60)
            for u in users:
                from datetime import datetime
                ts = datetime.fromtimestamp(u["created_at"]).strftime("%Y-%m-%d %H:%M")
                print(f"{u['username']:<20} {u['display_name']:<20} {ts}")

    elif args.command == "change-password":
        new_password = args.new_password or getpass.getpass("New password: ")
        _require_strong_password(new_password)
        ok = user_store.change_password(args.username, new_password)
        if ok:
            print(f"✓ Password changed for '{args.username}'.")
        else:
            print(f"✗ User '{args.username}' not found.", file=sys.stderr)
            sys.exit(1)

    elif args.command == "delete-user":
        ok = user_store.delete_user(args.username)
        if ok:
            print(f"✓ User '{args.username}' deleted.")
        else:
            print(f"✗ User '{args.username}' not found.", file=sys.stderr)
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
