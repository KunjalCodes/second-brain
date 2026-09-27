"""Requeue abandoned extraction jobs (Phase 11; docs/PHASE11_PLAN.md section 6.4).

    cd backend
    uv run python ../scripts/requeue_jobs.py --reason llm_unavailable            # dry run
    uv run python ../scripts/requeue_jobs.py --reason llm_unavailable --apply

For recovery after a provider outage or quota exhaustion: jobs that were
abandoned with a TRANSIENT reason are put back to `failed` with fresh attempts
and `next_attempt_at = now`, so the sweeper retries them. Their targets are
reopened too: the message goes back to `queued`, and the chunk goes back to
`pending` so its document returns to `extracting`.

Only reasons that say nothing about the job itself can be requeued (see
REQUEUEABLE). `llm_response_invalid` or a missing target would fail the same
way again. Dry run by default; prints counts only, never content.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.engine import Database  # noqa: E402

REQUEUEABLE = frozenset(
    {
        "llm_unavailable",
        "llm_rate_limited",
        "graph_unavailable",
        "graph_query_error",
        "database_unavailable",
        "database_error",
        "lease_expired",
        "internal_error",
    }
)

_SELECT = """
SELECT id FROM extraction_jobs
WHERE status = 'abandoned' AND error_code = :reason
  AND completed_at >= :since
ORDER BY completed_at
LIMIT :limit
"""


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--reason", required=True, choices=sorted(REQUEUEABLE))
    p.add_argument("--since-hours", type=float, default=72.0)
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    db = Database(get_settings())
    await db.connect()
    now = datetime.now(UTC)
    try:
        async with db.session() as session:
            ids = [
                row[0]
                for row in await session.execute(
                    text(_SELECT),
                    {
                        "reason": args.reason,
                        "since": now - timedelta(hours=args.since_hours),
                        "limit": args.limit,
                    },
                )
            ]
            print(f"{len(ids)} abandoned job(s) with error_code={args.reason!r}")
            if not ids or not args.apply:
                if ids:
                    print("dry run -- re-run with --apply to requeue")
                return 0
            await session.execute(
                text(
                    "UPDATE extraction_jobs SET status='failed', attempts=0, next_attempt_at=:now, "
                    "completed_at=NULL, locked_at=NULL, locked_by=NULL, "
                    "error_detail='requeued by operator' WHERE id = ANY(:ids)"
                ),
                {"now": now, "ids": ids},
            )
            await session.execute(
                text(
                    "UPDATE messages SET extraction_status='queued' "
                    "WHERE extraction_status='failed' AND id IN "
                    "(SELECT target_message_id FROM extraction_jobs WHERE id = ANY(:ids))"
                ),
                {"ids": ids},
            )
            chunks = await session.execute(
                text(
                    "UPDATE document_chunks SET extraction_status='pending' "
                    "WHERE extraction_status='failed' AND id IN "
                    "(SELECT target_chunk_id FROM extraction_jobs WHERE id = ANY(:ids)) "
                    "RETURNING document_id"
                ),
                {"ids": ids},
            )
            documents = sorted({row[0] for row in chunks})
            if documents:
                await session.execute(
                    text(
                        "UPDATE documents SET status='extracting', error_code=NULL, "
                        "error_detail=NULL WHERE id = ANY(:ids) "
                        "AND status IN ('failed', 'partially_complete')"
                    ),
                    {"ids": documents},
                )
            print(f"requeued {len(ids)} job(s); reopened {len(documents)} document(s)")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
