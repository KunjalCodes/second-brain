"""Operator CLI for accounts (Phase 9, docs/PHASE9_PLAN.md section 12; ADR 0021).

Runs directly against PostgreSQL (and, for `create --user-id`, reads Neo4j to
confirm the ownership id exists). There is no HTTP surface for any of this.

    uv run python ../scripts/manage_users.py create --email you@example.com
    uv run python ../scripts/manage_users.py create --email you@example.com --user-id demo-user
    uv run python ../scripts/manage_users.py disable --email you@example.com
    uv run python ../scripts/manage_users.py enable --email you@example.com
    uv run python ../scripts/manage_users.py reset-password --email you@example.com
    uv run python ../scripts/manage_users.py revoke-sessions --email you@example.com

(Run from `backend/` so `.env` is found.)

Passwords are NEVER accepted as command-line arguments (they would land in
shell history and `ps`). They are read with an interactive no-echo prompt,
or -- for automation -- as one line on stdin with `--password-stdin`.

`create --user-id <id>` ATTACHES a login to an EXISTING memory-ownership id
(e.g. the legacy `dev-user` / `demo-user`). It never moves, copies or rewrites
memory data, and it refuses:

* an id with no existing data (no `users` row and no Neo4j nodes) -- a new
  account should get a server-generated id (omit `--user-id`);
* an id that already has an account -- so it cannot reassign ownership of a
  user's memories to a different login.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.db.engine import Database  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402
from app.repositories.auth_repository import AuthRepository  # noqa: E402
from app.services.auth_service import DISABLED, AuthService  # noqa: E402

#: Same charset the document store enforces for a user directory name.
_SAFE_USER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

#: Read-only, operator-side existence check. Counts ONE node; not part of the
#: app's repository layer (the Cypher audit covers app/repositories only).
_NEO4J_HAS_DATA = "MATCH (n {user_id: $user_id}) RETURN count(n) > 0 AS found LIMIT 1"


def _read_password(args: argparse.Namespace, *, confirm: bool) -> str:
    if args.password_stdin:
        line = sys.stdin.readline()
        return line.rstrip("\r\n")
    first = getpass.getpass("Password: ")
    if confirm and getpass.getpass("Repeat password: ") != first:
        raise SystemExit("Passwords do not match.")
    return first


async def _ownership_id_exists(db: Database, user_id: str) -> tuple[bool, bool | None]:
    async with db.session() as session:
        in_postgres = await AuthRepository(session).user_exists(user_id)
    settings = get_settings()
    if not settings.neo4j_configured:
        return in_postgres, None
    graph = GraphClient(settings)
    try:
        await graph.connect()
        rows = await graph.read(_NEO4J_HAS_DATA, {"user_id": user_id})
        in_neo4j = bool(rows and rows[0]["found"])
    except AppError:
        in_neo4j = None  # unreachable: reported, not guessed
    finally:
        await graph.close()
    return in_postgres, in_neo4j


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    db = Database(settings)
    await db.connect()
    service = AuthService(db, settings)
    try:
        if args.command == "create":
            if args.user_id is not None:
                if not _SAFE_USER_ID.fullmatch(args.user_id):
                    print("ERROR: --user-id has invalid characters or length.", file=sys.stderr)
                    return 2
                in_pg, in_neo = await _ownership_id_exists(db, args.user_id)
                print(
                    f"ownership id {args.user_id!r}: postgres_users_row={in_pg} "
                    f"neo4j_data={'unknown (Neo4j unreachable)' if in_neo is None else in_neo}"
                )
                if not in_pg and not in_neo:
                    print(
                        "ERROR: no existing data under this id. Omit --user-id to create a "
                        "new account with a server-generated id.",
                        file=sys.stderr,
                    )
                    return 2
            password = _read_password(args, confirm=True)
            account = await service.create_account_for_user(
                email=args.email, password=password, user_id=args.user_id
            )
            print(f"created account email={account.email} user_id={account.user_id}")
        elif args.command in ("disable", "enable"):
            status = DISABLED if args.command == "disable" else "active"
            account = await service.set_status(email=args.email, status=status)
            suffix = " (all sessions revoked)" if status == DISABLED else ""
            print(f"account {account.email} is now {account.status}{suffix}")
        elif args.command == "reset-password":
            password = _read_password(args, confirm=True)
            revoked = await service.reset_password(email=args.email, new_password=password)
            print(f"password reset for {args.email}; revoked {revoked} session(s)")
        elif args.command == "revoke-sessions":
            revoked = await service.revoke_all(email=args.email)
            print(f"revoked {revoked} session(s) for {args.email}")
    except AppError as exc:
        print(f"ERROR: {exc.message}", file=sys.stderr)
        return 1
    finally:
        await db.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create an account (optionally for a legacy id)")
    create.add_argument("--email", required=True)
    create.add_argument("--user-id", default=None, help="attach to an EXISTING ownership id")
    create.add_argument("--password-stdin", action="store_true")
    for name in ("disable", "enable", "revoke-sessions"):
        p = sub.add_parser(name)
        p.add_argument("--email", required=True)
    reset = sub.add_parser("reset-password")
    reset.add_argument("--email", required=True)
    reset.add_argument("--password-stdin", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
