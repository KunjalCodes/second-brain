"""Backup, restore and verification (Phase 11, M9; docs/PHASE11_PLAN.md section 7).

    cd backend
    # 1. back up the stores the current environment points at
    uv run python ../scripts/backup.py create --out /secure/backups/2026-09-26
    # 2. restore into EMPTY targets (never over live data)
    uv run python ../scripts/backup.py restore --from /secure/backups/2026-09-26 \\
        --target-database-url postgresql://localhost/restored_db \\
        --target-neo4j-uri bolt://localhost:7688
    # 3. prove the restored copy matches the backup and is internally consistent
    uv run python ../scripts/backup.py verify --from /secure/backups/2026-09-26 \\
        --target-database-url ... --target-neo4j-uri ...

What a backup contains:

* `postgres.dump`  -- `pg_dump -Fc` of the whole database (users, accounts,
  sessions, conversations, messages, extraction/ingestion jobs and candidates,
  review queues + audit log, documents + chunks, embeddings, evaluation runs);
* `graph.nodes.jsonl` / `graph.rels.jsonl` -- every Neo4j node and
  relationship with typed properties (temporal values are tagged so they
  round-trip as Neo4j temporals, not strings). Plain Cypher only: no APOC, so
  it works on AuraDB Free;
* `documents.tar.gz` -- raw uploaded bytes still present in
  DOCUMENT_STORAGE_DIR (staging deletes them after ingestion, so usually few);
* `manifest.json`   -- row/node/relationship counts and SHA-256 of each file,
  written LAST, so a backup without a manifest is incomplete by definition.

`--encrypt` (or BACKUP_PASSPHRASE set) encrypts every file with
`openssl enc -aes-256-cbc -pbkdf2` using BACKUP_PASSPHRASE; restore decrypts.
Passphrases are read from the environment only, never from arguments.

The PostgreSQL and Neo4j snapshots are taken seconds apart, not atomically:
`verify` runs the cross-store consistency check to report any orphan
references a write between the two snapshots may have produced.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from neo4j import AsyncGraphDatabase  # noqa: E402
from neo4j.time import Date, DateTime, Duration, Time  # noqa: E402

from app.core.config import get_settings  # noqa: E402

PG_TABLES = (
    "users",
    "user_accounts",
    "auth_sessions",
    "conversations",
    "messages",
    "extraction_jobs",
    "extraction_candidates",
    "entity_merge_candidates",
    "memory_embeddings",
    "memory_review_candidates",
    "review_audit_log",
    "documents",
    "document_chunks",
    "ingestion_jobs",
    "evaluation_runs",
    "evaluation_task_results",
)
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BATCH = 500


# -- typed (de)serialization of Neo4j property values ----------------------------


def _encode(value: Any) -> Any:
    if isinstance(value, DateTime):
        return {"$t": "datetime", "v": value.iso_format()}
    if isinstance(value, Date):
        return {"$t": "date", "v": value.iso_format()}
    if isinstance(value, Time):
        return {"$t": "time", "v": value.iso_format()}
    if isinstance(value, Duration):
        return {"$t": "duration", "v": [value.months, value.days, value.seconds, value.nanoseconds]}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and "$t" in value:
        kind, raw = value["$t"], value["v"]
        if kind == "datetime":
            return DateTime.from_iso_format(raw)
        if kind == "date":
            return Date.from_iso_format(raw)
        if kind == "time":
            return Time.from_iso_format(raw)
        if kind == "duration":
            months, days, seconds, nanos = raw
            return Duration(months=months, days=days, seconds=seconds, nanoseconds=nanos)
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


# -- helpers --------------------------------------------------------------------


def _pg_tool(name: str) -> str:
    """`pg_dump`/`pg_restore` from PG_BIN_DIR when set. pg_dump refuses to dump
    a server newer than itself, so point this at the server's major version
    (e.g. /opt/homebrew/opt/postgresql@17/bin)."""
    bin_dir = os.environ.get("PG_BIN_DIR", "")
    return str(Path(bin_dir) / name) if bin_dir else name


def _libpq(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _passphrase() -> str | None:
    value = os.environ.get("BACKUP_PASSPHRASE", "")
    return value or None


def _openssl(args: list[str], src: Path, dst: Path) -> None:
    subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "200000",
            *args,
            "-in",
            str(src),
            "-out",
            str(dst),
            "-pass",
            "env:BACKUP_PASSPHRASE",
        ],
        check=True,
    )


async def _pg_counts(database_url: str) -> dict[str, int]:
    import asyncpg

    conn = await asyncpg.connect(_libpq(database_url))
    try:
        counts = {}
        for table in PG_TABLES:
            exists = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}")
            counts[table] = await conn.fetchval(f"SELECT count(*) FROM {table}") if exists else None  # noqa: S608
        counts["alembic_version"] = await conn.fetchval("SELECT version_num FROM alembic_version")
        return counts
    finally:
        await conn.close()


async def _graph_counts(driver) -> dict[str, dict[str, int]]:  # noqa: ANN001
    async with driver.session() as s:
        labels = await (
            await s.run("MATCH (n) UNWIND labels(n) AS l RETURN l, count(*) AS c")
        ).data()
        rels = await (await s.run("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c")).data()
        temporal = await (
            await s.run(
                "MATCH (m:Memory) RETURN count(m.valid_from) AS with_valid_from, "
                "count(m.valid_until) AS with_valid_until, "
                "count(CASE WHEN m.status <> 'active' THEN 1 END) AS not_active"
            )
        ).single()
    return {
        "labels": {r["l"]: r["c"] for r in labels},
        "relationships": {r["t"]: r["c"] for r in rels},
        "memory_temporal": dict(temporal) if temporal else {},
    }


def _driver(uri: str, user: str, password: str):  # noqa: ANN202
    return AsyncGraphDatabase.driver(uri, auth=(user, password))


# -- create -----------------------------------------------------------------------


async def create(out: Path, *, encrypt: bool) -> int:
    settings = get_settings()
    if not settings.database_url or not settings.neo4j_configured:
        print("ERROR: DATABASE_URL and NEO4J_* must be configured", file=sys.stderr)
        return 2
    if encrypt and not _passphrase():
        print("ERROR: --encrypt needs BACKUP_PASSPHRASE in the environment", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=False)
    started = datetime.now(UTC)

    pg_file = out / "postgres.dump"
    subprocess.run(
        [
            _pg_tool("pg_dump"),
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            f"--file={pg_file}",
            _libpq(settings.database_url),
        ],
        check=True,
    )
    pg_counts = await _pg_counts(settings.database_url)

    driver = _driver(settings.neo4j_uri, settings.neo4j_username, settings.neo4j_password)
    try:
        nodes_file, rels_file = out / "graph.nodes.jsonl", out / "graph.rels.jsonl"
        async with driver.session(database=settings.neo4j_database) as s:
            with nodes_file.open("w") as fh:
                result = await s.run(
                    "MATCH (n) RETURN elementId(n) AS eid, labels(n) AS labels, properties(n) AS p"
                )
                async for rec in result:
                    fh.write(
                        json.dumps(
                            {
                                "eid": rec["eid"],
                                "labels": rec["labels"],
                                "props": {k: _encode(v) for k, v in rec["p"].items()},
                            }
                        )
                        + "\n"
                    )
            with rels_file.open("w") as fh:
                result = await s.run(
                    "MATCH (a)-[r]->(b) RETURN elementId(a) AS s, elementId(b) AS e, "
                    "type(r) AS t, properties(r) AS p"
                )
                async for rec in result:
                    fh.write(
                        json.dumps(
                            {
                                "s": rec["s"],
                                "e": rec["e"],
                                "type": rec["t"],
                                "props": {k: _encode(v) for k, v in rec["p"].items()},
                            }
                        )
                        + "\n"
                    )
        graph_counts = await _graph_counts(driver)
    finally:
        await driver.close()

    docs_dir = Path(settings.document_storage_dir)
    docs_file = out / "documents.tar.gz"
    doc_files = 0
    with tarfile.open(docs_file, "w:gz") as tar:
        if docs_dir.is_dir():
            for path in sorted(docs_dir.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=str(path.relative_to(docs_dir)))
                    doc_files += 1

    files = [pg_file, nodes_file, rels_file, docs_file]
    if encrypt:
        encrypted = []
        for path in files:
            target = path.with_name(path.name + ".enc")
            _openssl([], path, target)
            path.unlink()
            encrypted.append(target)
        files = encrypted

    manifest = {
        "format": 1,
        "created_at": started.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "encrypted": encrypt,
        "postgres": pg_counts,
        "graph": graph_counts,
        "document_files": doc_files,
        "files": {p.name: {"sha256": _sha256(p), "bytes": p.stat().st_size} for p in files},
        "not_included": [
            "LLM provider state (none is kept)",
            "Neo4j constraints/indexes (re-created by scripts/init_graph.py)",
            "rate-limit counters are included but disposable",
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    print(json.dumps({"backup": str(out), "files": manifest["files"]}, indent=2))
    return 0


# -- restore ----------------------------------------------------------------------


def _materialize(src: Path, work: Path, name: str, encrypted: bool) -> Path:
    if not encrypted:
        return src / name
    if not _passphrase():
        raise SystemExit("ERROR: encrypted backup; set BACKUP_PASSPHRASE")
    target = work / name
    _openssl(["-d"], src / f"{name}.enc", target)
    return target


async def restore(
    src: Path,
    *,
    database_url: str,
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
    neo4j_database: str,
    documents_dir: str | None,
) -> int:
    manifest = json.loads((src / "manifest.json").read_text())
    for name, meta in manifest["files"].items():
        if _sha256(src / name) != meta["sha256"]:
            print(f"ERROR: checksum mismatch for {name}", file=sys.stderr)
            return 2
    work = src / ".restore-tmp"
    work.mkdir(exist_ok=True)
    try:
        enc = manifest["encrypted"]
        import asyncpg

        conn = await asyncpg.connect(_libpq(database_url))
        try:
            tables = await conn.fetchval(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
            )
        finally:
            await conn.close()
        if tables:
            print("ERROR: target PostgreSQL database is not empty; refusing", file=sys.stderr)
            return 2
        pg = _materialize(src, work, "postgres.dump", enc)
        subprocess.run(
            [
                _pg_tool("pg_restore"),
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                f"--dbname={_libpq(database_url)}",
                str(pg),
            ],
            check=True,
        )

        driver = _driver(neo4j_uri, neo4j_user, neo4j_password)
        try:
            async with driver.session(database=neo4j_database) as s:
                existing = await (await s.run("MATCH (n) RETURN count(n) AS c")).single()
                if existing["c"]:
                    print("ERROR: target Neo4j database is not empty; refusing", file=sys.stderr)
                    return 2
                await s.run(
                    "CREATE INDEX restore_eid IF NOT EXISTS FOR (n:_Restore) ON (n._restore_eid)"
                )
                await s.run("CALL db.awaitIndexes(60)")
                groups: dict[tuple[str, ...], list[dict]] = {}
                with _materialize(src, work, "graph.nodes.jsonl", enc).open() as fh:
                    for line in fh:
                        row = json.loads(line)
                        labels = tuple(sorted(row["labels"]))
                        if not all(_NAME.match(label) for label in labels):
                            raise SystemExit(f"ERROR: unsafe label in backup: {labels}")
                        groups.setdefault(labels, []).append(
                            {
                                "eid": row["eid"],
                                "props": {k: _decode(v) for k, v in row["props"].items()},
                            }
                        )
                for labels, rows in groups.items():
                    label_expr = "".join(f":`{label}`" for label in labels)
                    for i in range(0, len(rows), _BATCH):
                        await s.run(
                            f"UNWIND $rows AS row CREATE (n:_Restore{label_expr}) "
                            "SET n = row.props, n._restore_eid = row.eid",
                            rows=rows[i : i + _BATCH],
                        )
                rel_groups: dict[str, list[dict]] = {}
                with _materialize(src, work, "graph.rels.jsonl", enc).open() as fh:
                    for line in fh:
                        row = json.loads(line)
                        if not _NAME.match(row["type"]):
                            raise SystemExit(f"ERROR: unsafe relationship type: {row['type']}")
                        rel_groups.setdefault(row["type"], []).append(
                            {
                                "s": row["s"],
                                "e": row["e"],
                                "props": {k: _decode(v) for k, v in row["props"].items()},
                            }
                        )
                for rel_type, rows in rel_groups.items():
                    for i in range(0, len(rows), _BATCH):
                        await s.run(
                            "UNWIND $rows AS row "
                            "MATCH (a:_Restore {_restore_eid: row.s}), "
                            "(b:_Restore {_restore_eid: row.e}) "
                            f"CREATE (a)-[r:`{rel_type}`]->(b) SET r = row.props",
                            rows=rows[i : i + _BATCH],
                        )
                await s.run("MATCH (n:_Restore) REMOVE n:_Restore, n._restore_eid")
                await s.run("DROP INDEX restore_eid IF EXISTS")
        finally:
            await driver.close()

        if documents_dir:
            with tarfile.open(_materialize(src, work, "documents.tar.gz", enc)) as tar:
                tar.extractall(documents_dir, filter="data")
    finally:
        for path in work.glob("*"):
            path.unlink()
        work.rmdir()
    print("restore complete; now run scripts/init_graph.py against the target and `verify`")
    return 0


# -- verify -------------------------------------------------------------------------


async def verify(
    src: Path, *, database_url: str, neo4j_uri: str, neo4j_user: str, neo4j_password: str
) -> int:
    from check_consistency import check

    manifest = json.loads((src / "manifest.json").read_text())
    report: dict[str, Any] = {"postgres": {}, "graph": {}}
    ok = True
    restored_pg = await _pg_counts(database_url)
    for key, expected in manifest["postgres"].items():
        match = restored_pg.get(key) == expected
        ok &= match
        report["postgres"][key] = {
            "expected": expected,
            "restored": restored_pg.get(key),
            "ok": match,
        }
    driver = _driver(neo4j_uri, neo4j_user, neo4j_password)
    try:
        restored_graph = await _graph_counts(driver)
    finally:
        await driver.close()
    for section in ("labels", "relationships", "memory_temporal"):
        expected = manifest["graph"][section]
        got = restored_graph[section]
        match = expected == got
        ok &= match
        report["graph"][section] = {"expected": expected, "restored": got, "ok": match}
    consistency = await check(
        database_url=database_url,
        neo4j_uri=neo4j_uri,
        neo4j_user=neo4j_user,
        neo4j_password=neo4j_password,
    )
    report["consistency"] = consistency
    ok &= consistency["orphans_total"] == 0
    report["ok"] = ok
    print(json.dumps(report, indent=2, default=str))
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--encrypt", action="store_true")
    for name in ("restore", "verify"):
        r = sub.add_parser(name)
        r.add_argument("--from", dest="src", type=Path, required=True)
        r.add_argument("--target-database-url", required=True)
        r.add_argument("--target-neo4j-uri", required=True)
        r.add_argument("--target-neo4j-user", default="neo4j")
        r.add_argument("--target-neo4j-database", default="neo4j")
        if name == "restore":
            r.add_argument("--target-documents-dir", default=None)
    args = p.parse_args()
    target_password = os.environ.get("TARGET_NEO4J_PASSWORD", "")
    if args.cmd == "create":
        return asyncio.run(create(args.out, encrypt=args.encrypt or bool(_passphrase())))
    if not target_password:
        print("ERROR: set TARGET_NEO4J_PASSWORD in the environment", file=sys.stderr)
        return 2
    if args.cmd == "restore":
        return asyncio.run(
            restore(
                args.src,
                database_url=args.target_database_url,
                neo4j_uri=args.target_neo4j_uri,
                neo4j_user=args.target_neo4j_user,
                neo4j_password=target_password,
                neo4j_database=args.target_neo4j_database,
                documents_dir=args.target_documents_dir,
            )
        )
    return asyncio.run(
        verify(
            args.src,
            database_url=args.target_database_url,
            neo4j_uri=args.target_neo4j_uri,
            neo4j_user=args.target_neo4j_user,
            neo4j_password=target_password,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
