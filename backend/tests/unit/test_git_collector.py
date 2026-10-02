# ruff: noqa: E501 - compact fixtures and tables
"""Phase 16: the local read-only Git collector, against REAL temporary repositories."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from app.projects.git_schemas import GitBatch, StructureBatch
from app.projects.naming import normalize_path as server_normalize_path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "git_collect.py"
spec = importlib.util.spec_from_file_location("git_collect", SCRIPT)
gc = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(gc)  # type: ignore[union-attr]

SECRET_EMAIL = "ada.lovelace@example.com"


def run(repo: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_DATE": "2026-01-10T10:00:00Z", "GIT_COMMITTER_DATE": "2026-01-10T10:00:00Z"}
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        env={**__import__("os").environ, **env},
    )  # fmt: skip
    return done.stdout


def commit(repo: Path, message: str, date: str) -> str:
    import os

    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A", "--", "."], check=True, env=env, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", message],
        check=True,
        env=env,
        capture_output=True,
    )
    return run(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "work"
    r.mkdir()
    run(r, "init", "-q", "-b", "main")
    run(r, "config", "user.name", "Ada Lovelace")
    run(r, "config", "user.email", SECRET_EMAIL)
    run(
        r,
        "remote",
        "add",
        "origin",
        "https://ci-user:s3cr3t-token@github.com/Acme/Auth-Service.git",
    )
    (r / "src" / "auth").mkdir(parents=True)
    (r / "src" / "auth" / "__init__.py").write_text("")
    (r / "src" / "auth" / "session.py").write_text(
        "import os\nimport requests\nfrom src import config\n\n\nclass Session:\n    def create(self):\n        pass\n\n    class Inner:\n        def deep(self):\n            def nested():\n                pass\n\n\ndef helper():\n    pass\n\n\nasync def fetch():\n    pass\n"
    )
    (r / "src" / "config.py").write_text("TTL = 15\n")
    (r / "src" / "app.py").write_text(
        "from .auth.session import Session\nfrom src.auth import session\nimport yaml\nimport json\n"
    )
    (r / "README.md").write_text("# Demo\n")
    commit(r, "Initial commit\n\nLong body here.", "2026-01-01T10:00:00Z")
    (r / "src" / "config.py").write_text("TTL = 60\nDEBUG = False\n")
    commit(r, "Extend TTL", "2026-01-02T10:00:00Z")
    run(r, "mv", "README.md", "docs.md")
    commit(r, "Rename readme", "2026-01-03T10:00:00Z")
    run(r, "checkout", "-q", "-b", "feature/x")
    (r / "src" / "feature.py").write_text("def f():\n    pass\n")
    commit(r, "Add feature", "2026-01-04T10:00:00Z")
    return r


# -- safety ------------------------------------------------------------------------------------


def test_identity_strips_credentials_and_never_returns_a_path(repo: Path) -> None:
    ident = gc.repository_identity(str(repo))
    assert ident["repository_url"] == "https://github.com/Acme/Auth-Service.git"
    assert "s3cr3t" not in json.dumps(ident) and "ci-user" not in json.dumps(ident)
    assert len(ident["root_commits"]) == 1 and len(ident["root_commits"][0]) == 40


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://u:p@host.com/o/r.git", "https://host.com/o/r.git"),
        ("https://tok@host.com:8443/o/r", "https://host.com:8443/o/r"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("ssh://git:pw@host.com/o/r", "ssh://host.com/o/r"),
    ],
)
def test_credentials_are_stripped_from_urls(raw: str, expected: str) -> None:
    assert gc.strip_credentials(raw) == expected


def test_a_remote_that_is_a_local_path_is_not_sent(tmp_path: Path) -> None:
    r = tmp_path / "r"
    r.mkdir()
    run(r, "init", "-q", "-b", "main")
    run(r, "config", "user.name", "x")
    run(r, "config", "user.email", "x@x.x")
    run(r, "remote", "add", "origin", str(tmp_path / "somewhere"))
    (r / "a.txt").write_text("a")
    commit(r, "c", "2026-01-01T10:00:00Z")
    ident = gc.repository_identity(str(r))
    assert "repository_url" not in ident and ident["root_commits"]


def test_no_raw_email_and_no_absolute_path_ever_leaves_the_machine(repo: Path) -> None:
    commits, branches = gc.collect_commits(str(repo), 50)
    _, files, _ = gc.collect_structure(str(repo), "HEAD", gc.DEFAULT_EXCLUDES, 100)
    wire = json.dumps(
        {
            "commits": commits,
            "branches": branches,
            "files": files,
            "id": gc.repository_identity(str(repo)),
        }
    )
    assert SECRET_EMAIL not in wire and "lovelace@" not in wire
    assert str(repo) not in wire and "/Users" not in wire and str(repo.parent) not in wire


def test_hostile_filenames_are_data_never_commands(repo: Path) -> None:
    sneaky = [
        "a; touch PWNED.py",
        "$(touch PWNED2).py",
        "-rf.py",
        "--version.py",
        "sp ace.py",
        "ünï.py",
        "`touch PWNED3`.py",
    ]
    for name in sneaky:
        (repo / name).write_text("x = 1\n")
    sha = commit(repo, "weird names", "2026-01-05T10:00:00Z")
    commits, _ = gc.collect_commits(str(repo), 50)
    listed = {f["path"] for c in commits if c["sha"] == sha for f in c["files"]}
    assert set(sneaky) <= listed
    _, files, _ = gc.collect_structure(str(repo), "HEAD", [], 100)
    assert set(sneaky) <= {f["path"] for f in files}
    assert not list(repo.glob("PWNED*")) and not list(repo.parent.glob("PWNED*"))


@pytest.mark.parametrize("bad", ["--output=/tmp/x", "-n1", "a b", "x;y", "$(id)", "a" * 101])
def test_unsafe_revision_arguments_are_refused(repo: Path, bad: str) -> None:
    with pytest.raises(gc.CollectorError):
        gc.collect_commits(str(repo), 5, since=bad)
    with pytest.raises(gc.CollectorError):
        gc.collect_structure(str(repo), bad, [], 10)


def test_an_empty_revision_is_refused() -> None:
    with pytest.raises(gc.CollectorError):
        gc.safe_rev("")


def test_the_collector_only_runs_read_only_git_subcommands() -> None:
    import re

    source = SCRIPT.read_text()
    verbs = set(re.findall(r'git\(\s*(?:repo|args\.repo|str\(repo\)),\s*"([a-z-]+)"', source))
    verbs |= set(re.findall(r'"-C", repo, "([a-z-]+)"', source))
    verbs -= {"-c"}  # `-c key=value` configuration, not a subcommand
    assert verbs <= {
        "rev-parse",
        "config",
        "remote",
        "rev-list",
        "for-each-ref",
        "log",
        "ls-tree",
        "cat-file",
    }, verbs
    assert "shell=True" not in source and "os.system" not in source and "eval(" not in source


def test_not_a_repository_is_a_clean_error(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert gc.main(["identity", "--repo", str(tmp_path)]) == 1
    assert "not inside a git work tree" in capsys.readouterr().err


# -- commits -----------------------------------------------------------------------------------


def test_commit_metadata_is_complete_and_bounded(repo: Path) -> None:
    commits, branches = gc.collect_commits(str(repo), 50)
    by_subject = {c["subject"]: c for c in commits}
    assert set(by_subject) == {"Initial commit", "Extend TTL", "Rename readme", "Add feature"}
    first = by_subject["Initial commit"]
    assert (
        first["parents"] == []
        and first["body"] == "Long body here."
        and first["author_name"] == "Ada Lovelace"
    )
    assert len(first["author_email_sha256"]) == 64 and first["committed_at"].startswith(
        "2026-01-01"
    )
    second = by_subject["Extend TTL"]
    assert second["parents"] == [first["sha"]]
    (cfg,) = second["files"]
    assert cfg == {"path": "src/config.py", "status": "M", "additions": 2, "deletions": 1}
    assert (
        second["additions"] == 2 and second["deletions"] == 1 and second["files_changed_count"] == 1
    )
    rename = by_subject["Rename readme"]["files"][0]
    assert (
        rename["status"] == "R"
        and rename["old_path"] == "README.md"
        and rename["path"] == "docs.md"
    )
    assert {b["name"] for b in branches} == {"main", "feature/x"}
    assert by_subject["Add feature"]["branches"] == ["feature/x"]
    assert sorted(by_subject["Initial commit"]["branches"]) == ["feature/x", "main"]


def test_max_commits_and_since_bound_the_walk(repo: Path) -> None:
    assert len(gc.collect_commits(str(repo), 2)[0]) == 2
    everything = gc.collect_commits(str(repo), 50)[0]
    root = next(c for c in everything if c["subject"] == "Initial commit")["sha"]
    assert len(gc.collect_commits(str(repo), 50, since=root)[0]) == 3
    assert (
        len(gc.collect_commits(str(repo), 10**9)[0]) == 4
    )  # clamped to the hard cap, not unbounded


def test_a_huge_commit_is_truncated_honestly(repo: Path) -> None:
    for i in range(gc.MAX_FILES_PER_COMMIT + 25):
        (repo / f"bulk_{i}.txt").write_text(str(i))
    sha = commit(repo, "bulk", "2026-01-06T10:00:00Z")
    c = next(c for c in gc.collect_commits(str(repo), 50)[0] if c["sha"] == sha)
    assert (
        len(c["files"]) == gc.MAX_FILES_PER_COMMIT
        and c["files_changed_count"] == gc.MAX_FILES_PER_COMMIT + 25
    )
    assert c["files_truncated"] is True


def test_unrepresentable_paths_are_counted_but_not_sent(repo: Path) -> None:
    long_name = "d/" + "x" * 195 + ".py"
    (repo / "d").mkdir()
    (repo / long_name).write_text("x = 1\n")
    sha = commit(repo, "long path", "2026-01-07T10:00:00Z")
    c = next(c for c in gc.collect_commits(str(repo), 50)[0] if c["sha"] == sha)
    assert c["files"] == [] and c["files_changed_count"] == 1 and c["files_truncated"] is True


def test_long_bodies_are_cut_and_flagged(repo: Path) -> None:
    (repo / "z.txt").write_text("z")
    sha = commit(repo, "subject\n\n" + ("word " * 2000), "2026-01-08T10:00:00Z")
    c = next(c for c in gc.collect_commits(str(repo), 50)[0] if c["sha"] == sha)
    assert len(c["body"]) <= gc.MAX_BODY_CHARS and c["body_truncated"] is True


def test_collected_commits_validate_against_the_servers_schema(repo: Path) -> None:
    commits, branches = gc.collect_commits(str(repo), 50)
    batch = GitBatch.model_validate(
        {"identity": gc.repository_identity(str(repo)), "branches": branches, "commits": commits}
    )
    assert len(batch.commits) == 4


# -- structure ---------------------------------------------------------------------------------


def test_python_structure_symbols_imports_and_dependencies(repo: Path) -> None:
    sha, files, _ = gc.collect_structure(str(repo), "HEAD", gc.DEFAULT_EXCLUDES, 100)
    by = {f["path"]: f for f in files}
    assert sha == run(repo, "rev-parse", "HEAD").strip()
    session = by["src/auth/session.py"]
    assert session["language"] == "python"
    assert {(s["name"], s["kind"]) for s in session["symbols"]} == {
        ("Session", "class"), ("Session.create", "method"), ("Session.Inner", "class"),
        ("Session.Inner.deep", "method"), ("helper", "function"), ("fetch", "function"),
    }  # fmt: skip  # nested-in-function defs are not symbols
    assert session["dependencies"] == ["requests"]  # `os` is stdlib; `src` resolves inside the repo
    assert "src/config.py" in session["imports"]
    app = by["src/app.py"]
    assert (
        set(app["imports"]) == {"src/auth/session.py", "src/auth/__init__.py"}
        or "src/auth/session.py" in app["imports"]
    )
    assert app["dependencies"] == ["yaml"]  # json is stdlib
    assert by["docs.md"]["language"] == "markdown" and "symbols" not in by["docs.md"]


def test_unparseable_python_yields_no_symbols_not_guesses(repo: Path) -> None:
    (repo / "broken.py").write_text("def (:\n   class\n")
    commit(repo, "broken", "2026-01-09T10:00:00Z")
    _, files, _ = gc.collect_structure(str(repo), "HEAD", [], 100)
    broken = next(f for f in files if f["path"] == "broken.py")
    assert "symbols" not in broken or broken["symbols"] == []


def test_oversized_python_is_listed_but_not_parsed(repo: Path) -> None:
    (repo / "big.py").write_text("x = 1\n" * (gc.MAX_PY_BYTES // 6 + 100))
    commit(repo, "big", "2026-01-09T11:00:00Z")
    _, files, _ = gc.collect_structure(str(repo), "HEAD", [], 100)
    big = next(f for f in files if f["path"] == "big.py")
    assert big["size_bytes"] > gc.MAX_PY_BYTES and "symbols" not in big


def test_excludes_and_limits_apply(repo: Path) -> None:
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "i.js").write_text("x")
    (repo / "logo.png").write_bytes(b"\x89PNG")
    commit(repo, "assets", "2026-01-09T12:00:00Z")
    _, files, skipped = gc.collect_structure(str(repo), "HEAD", gc.DEFAULT_EXCLUDES, 100)
    paths = {f["path"] for f in files}
    assert "node_modules/pkg/i.js" not in paths and "logo.png" not in paths and skipped >= 2
    _, limited, skipped2 = gc.collect_structure(str(repo), "HEAD", gc.DEFAULT_EXCLUDES, 3)
    assert len(limited) == 3 and skipped2 > skipped


def test_structure_reads_the_committed_tree_not_the_working_copy(repo: Path) -> None:
    (repo / "src" / "uncommitted.py").write_text("def nope():\n    pass\n")
    _, files, _ = gc.collect_structure(str(repo), "HEAD", [], 100)
    assert "src/uncommitted.py" not in {f["path"] for f in files}


def test_collected_structure_validates_against_the_servers_schema(repo: Path) -> None:
    sha, files, _ = gc.collect_structure(str(repo), "HEAD", gc.DEFAULT_EXCLUDES, 100)
    StructureBatch.model_validate(
        {"identity": gc.repository_identity(str(repo)), "commit_sha": sha, "files": files}
    )


# -- agreement with the server's rules ---------------------------------------------------------

CORPUS = [
    "src/a.py", "./src//a.py", "a/./b", "../x", "/abs", "~/x", "C:/x", "a\\b", ".git/config", "x" * 191,
    "a/" + "b" * 256, "", " ", "a\x00b", "a\nb", "ünï/файл.py", "x\u202ey", "a/..", "...", ".hidden/x",
]  # fmt: skip


@pytest.mark.parametrize("raw", CORPUS)
def test_collector_and_server_agree_on_every_path(raw: str) -> None:
    from app.core.errors import ValidationError

    mine = gc.normalize_path(raw)
    try:
        theirs: str | None = server_normalize_path(raw)
    except ValidationError:
        theirs = None
    assert mine == theirs, raw


# -- the CLI -----------------------------------------------------------------------------------


def test_dry_run_reads_and_sends_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(gc, "post", lambda *a, **k: pytest.fail("a dry run must not send"))
    assert gc.main(["all", "--repo", str(repo), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "collected 4 commit(s)" in out and "structure of" in out


def test_the_token_comes_only_from_the_environment_and_is_never_printed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    secret = "sbp_" + "S" * 43
    monkeypatch.setenv("MEMORY_ENGINE_TOKEN", secret)
    sent: list[tuple] = []

    def fake_post(server, token, path, body, retries=3):  # noqa: ANN001, ANN202
        sent.append((path, token, body))
        if path.endswith("/git/commits"):
            return {
                "results": [
                    {"sha": c["sha"], "status": "recorded", "projection_status": "projected"}
                    for c in body["commits"]
                ]
            }
        return {
            "memories_created": 1,
            "memories_existing": 0,
            "memories_superseded": 0,
            "files_stale": 0,
        }

    monkeypatch.setattr(gc, "post", fake_post)
    assert (
        gc.main(["all", "--repo", str(repo), "--server", "http://x.test", "--project", "current"])
        == 0
    )
    captured = capsys.readouterr()
    assert secret not in captured.out and secret not in captured.err
    assert {p for p, _, _ in sent} == {
        "/projects/current/git/commits",
        "/projects/current/git/structure",
    }
    assert all(t == secret for _, t, _ in sent)
    assert all("token" not in json.dumps(b) for _, _, b in sent)
    commits_sent = [c["subject"] for p, _, b in sent if p.endswith("commits") for c in b["commits"]]
    assert commits_sent[0] == "Initial commit"  # oldest first: parents precede children


def test_missing_token_or_bad_arguments_fail_cleanly(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv("MEMORY_ENGINE_TOKEN", raising=False)
    assert (
        gc.main(["commits", "--repo", str(repo)]) == 1
        and "MEMORY_ENGINE_TOKEN" in capsys.readouterr().err
    )
    monkeypatch.setenv("MEMORY_ENGINE_TOKEN", "t")
    assert gc.main(["commits", "--repo", str(repo), "--server", "file:///etc/passwd"]) == 1
    assert gc.main(["commits", "--repo", str(repo), "--project", "../../x"]) == 1
