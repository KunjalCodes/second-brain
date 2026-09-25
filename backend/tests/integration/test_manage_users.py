"""The Phase 9 operator CLI (scripts/manage_users.py) against real PostgreSQL
and Neo4j: attaching a login to an EXISTING legacy ownership id without
touching its data, and the refusals that keep it from reassigning ownership.

A synthetic `legacy-<uuid>` id stands in for `dev-user`/`demo-user`: the real
ids are never touched by tests."""

from __future__ import annotations

import importlib.util
import io
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.errors import AuthenticationRequiredError
from app.services.auth_service import AuthService

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "manage_users.py"
PASSWORD = "operator chosen passphrase"


def _cli():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("manage_users", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def legacy_id() -> str:
    return f"legacy-{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def cleanup_ids(db, graph):  # noqa: ANN001, ANN201
    ids: list[str] = []
    yield ids
    async with db.session() as session:
        for uid in ids:
            for stmt in (
                "DELETE FROM auth_sessions WHERE user_id = :u",
                "DELETE FROM user_accounts WHERE user_id = :u",
                "DELETE FROM users WHERE id = :u",
            ):
                await session.execute(text(stmt), {"u": uid})
    for uid in ids:
        await graph.write("MATCH (n {user_id: $u}) DETACH DELETE n", {"u": uid})


async def _run(cli, argv: list[str], monkeypatch: pytest.MonkeyPatch, stdin: str = "") -> int:  # noqa: ANN001
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return await cli.run(cli.build_parser().parse_args(argv))


async def test_attaches_an_account_to_a_legacy_neo4j_only_id(
    db,
    graph,
    legacy_id,
    cleanup_ids,
    monkeypatch,
    capsys,  # noqa: ANN001
) -> None:
    cleanup_ids.append(legacy_id)
    await graph.write(
        "CREATE (:Memory {id: $m, user_id: $u, content: 'legacy memory'})",
        {"m": str(uuid.uuid4()), "u": legacy_id},
    )
    cli = _cli()
    code = await _run(
        cli,
        ["create", "--email", "Legacy@Example.com", "--user-id", legacy_id, "--password-stdin"],
        monkeypatch,
        stdin=PASSWORD + "\n",
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert f"user_id={legacy_id}" in out and PASSWORD not in out
    account = await AuthService(db, get_settings()).login(
        email="legacy@example.com", password=PASSWORD
    )
    assert account.account.user_id == legacy_id
    rows = await graph.read("MATCH (n {user_id: $u}) RETURN count(n) AS c", {"u": legacy_id})
    assert rows[0]["c"] == 1  # the memory data was not touched


async def test_refuses_an_id_with_no_existing_data(
    db,
    legacy_id,
    cleanup_ids,
    monkeypatch,
    capsys,  # noqa: ANN001
) -> None:
    cleanup_ids.append(legacy_id)
    code = await _run(
        _cli(),
        [
            "create",
            "--email",
            f"{legacy_id}@example.com",
            "--user-id",
            legacy_id,
            "--password-stdin",
        ],
        monkeypatch,
        stdin=PASSWORD + "\n",
    )
    assert code == 2
    async with db.session() as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM users WHERE id = :u"), {"u": legacy_id}
            )
        ).scalar_one() == 0


async def test_refuses_to_reassign_an_id_that_already_has_an_account(
    db,
    graph,
    legacy_id,
    cleanup_ids,
    monkeypatch,  # noqa: ANN001
) -> None:
    cleanup_ids.append(legacy_id)
    await graph.write(
        "CREATE (:Entity {id: $e, user_id: $u})", {"e": str(uuid.uuid4()), "u": legacy_id}
    )
    cli = _cli()
    first = await _run(
        cli,
        ["create", "--email", f"a-{legacy_id}@x.io", "--user-id", legacy_id, "--password-stdin"],
        monkeypatch,
        stdin=PASSWORD + "\n",
    )
    second = await _run(
        cli,
        ["create", "--email", f"b-{legacy_id}@x.io", "--user-id", legacy_id, "--password-stdin"],
        monkeypatch,
        stdin=PASSWORD + "\n",
    )
    assert (first, second) == (0, 1)
    async with db.session() as session:
        emails = (
            (
                await session.execute(
                    text("SELECT email_normalized FROM user_accounts WHERE user_id = :u"),
                    {"u": legacy_id},
                )
            )
            .scalars()
            .all()
        )
    assert emails == [f"a-{legacy_id}@x.io"]


async def test_rejects_unsafe_user_id_characters(monkeypatch) -> None:  # noqa: ANN001
    code = await _run(
        _cli(),
        ["create", "--email", "x@y.io", "--user-id", "../../etc", "--password-stdin"],
        monkeypatch,
        stdin=PASSWORD + "\n",
    )
    assert code == 2


async def test_disable_enable_reset_and_revoke(db, cleanup_ids, monkeypatch, capsys) -> None:  # noqa: ANN001
    cli = _cli()
    email = f"op-{uuid.uuid4().hex[:8]}@example.com"
    assert (
        await _run(
            cli,
            ["create", "--email", email, "--password-stdin"],
            monkeypatch,
            stdin=PASSWORD + "\n",
        )
        == 0
    )
    service = AuthService(db, get_settings())
    cleanup_ids.append((await service.account(email)).user_id)
    login = await service.login(email=email, password=PASSWORD)

    assert await _run(cli, ["disable", "--email", email], monkeypatch) == 0
    with pytest.raises(AuthenticationRequiredError):
        await service.authenticate(login.token)  # disabling revoked it
    assert await _run(cli, ["enable", "--email", email], monkeypatch) == 0

    new = "a replacement passphrase"
    assert (
        await _run(
            cli,
            ["reset-password", "--email", email, "--password-stdin"],
            monkeypatch,
            stdin=new + "\n",
        )
        == 0
    )
    fresh = await service.login(email=email, password=new)
    assert await _run(cli, ["revoke-sessions", "--email", email], monkeypatch) == 0
    with pytest.raises(AuthenticationRequiredError):
        await service.authenticate(fresh.token)
    assert PASSWORD not in capsys.readouterr().out


def test_the_cli_never_accepts_a_password_argument() -> None:
    parser = _cli().build_parser()
    for command in ("create", "reset-password"):
        with pytest.raises(SystemExit):
            parser.parse_args([command, "--email", "x@y.io", "--password", "leaked"])
