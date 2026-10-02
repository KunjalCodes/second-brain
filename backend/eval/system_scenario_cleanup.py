"""Remove the SYNTHETIC data `eval/system_scenario.py` (and the Phase 17 probes) created.

    uv run python eval/system_scenario_cleanup.py            # dry run: only counts
    uv run python eval/system_scenario_cleanup.py --apply    # delete

Scope is deliberately narrow: accounts whose email matches `p17-%@example.com`, the
projects they own, and those projects' memory spaces (`proj_<hex>`), in PostgreSQL and
Neo4j. It never touches `eval-agent-*` or any other account, and refuses an email
pattern that is not a `p17-` one. Run it against the SAME environment the scenario used.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import bindparam, text  # noqa: E402

from app.db.engine import Database  # noqa: E402
from app.graph.connection import GraphClient  # noqa: E402

EMAIL_PATTERN = "p17-%@example.com"


def q(sql: str, *lists: str):  # noqa: ANN201
    """`sql` with each named parameter bound as an expanding IN list."""
    return text(sql).bindparams(*[bindparam(n, expanding=True) for n in lists])


async def main(apply: bool) -> int:
    db, graph = Database(), GraphClient()
    await db.connect()
    await graph.connect()
    try:
        async with db.session() as s:
            owners = [
                r[0]
                for r in (
                    await s.execute(
                        text("SELECT user_id FROM user_accounts WHERE email_normalized LIKE :p"),
                        {"p": EMAIL_PATTERN},
                    )
                ).all()
            ]
            if not owners:
                print("nothing to clean: no p17-* accounts")
                return 0
            projects = (
                await s.execute(
                    q("SELECT id::text, space_id FROM projects WHERE owner_user_id IN :o", "o"),
                    {"o": owners},
                )
            ).all()
            spaces = [p[1] for p in projects]
            scopes = owners + spaces
            tables = [
                r[0]
                for r in (
                    await s.execute(
                        text(
                            "SELECT table_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND column_name='user_id' "
                            "AND table_name <> 'user_accounts' ORDER BY 1"
                        )
                    )
                ).all()
            ]
            print(f"accounts={len(owners)} projects={len(projects)} spaces={len(spaces)}")
            if not apply:
                for t in tables:
                    n = (
                        await s.execute(
                            q(f"SELECT count(*) FROM {t} WHERE user_id IN :u", "u"),  # noqa: S608
                            {"u": scopes},
                        )
                    ).scalar_one()
                    if n:
                        print(f"  {t}: {n} rows")
                nodes = await graph.read(
                    "MATCH (n) WHERE n.user_id IN $u RETURN count(n) AS n", {"u": scopes}
                )
                print(f"  neo4j nodes: {nodes[0]['n']}")
                print("dry run: nothing deleted (pass --apply)")
                return 0
            for t in tables:
                await s.execute(
                    q(f"DELETE FROM {t} WHERE user_id IN :u", "u"),  # noqa: S608
                    {"u": scopes},
                )
            await s.execute(
                q("DELETE FROM project_tokens WHERE CAST(project_id AS text) IN :p", "p"),
                {"p": [p[0] for p in projects]},
            )
            for sql, key, vals in (
                ("DELETE FROM projects WHERE owner_user_id IN :o", "o", owners),
                ("DELETE FROM user_accounts WHERE user_id IN :o", "o", owners),
                ("DELETE FROM users WHERE id IN :o", "o", scopes),
            ):
                await s.execute(q(sql, key), {key: vals})
        await graph.write("MATCH (n) WHERE n.user_id IN $u DETACH DELETE n", {"u": scopes})
        print("deleted")
        return 0
    finally:
        await graph.close()
        await db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().apply)))
