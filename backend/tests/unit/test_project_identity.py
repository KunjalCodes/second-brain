"""Phase 16 project identity: one repository, one key; never a filesystem path."""

from __future__ import annotations

import pytest

from app.auth.tokens import (
    PROJECT_TOKEN_PREFIX,
    is_project_token,
    is_well_formed,
    is_well_formed_project_token,
    new_project_token,
    new_session_token,
    token_digest,
)
from app.core.errors import ValidationError
from app.projects.identity import (
    derive_identity,
    normalize_repository_url,
    normalize_root_commits,
)

CANONICAL = "github.com/owner/repo"


@pytest.mark.parametrize(
    "raw",
    [
        "https://github.com/owner/repo",
        "https://github.com/owner/repo.git",
        "https://github.com/Owner/Repo.git/",
        "HTTPS://GitHub.com/owner/repo",
        "https://user:p%40ss@github.com/owner/repo.git",
        "https://x-access-token:ghp_secret@github.com/owner/repo",
        "git@github.com:owner/repo.git",
        "github.com:owner/repo",
        "ssh://git@github.com/owner/repo.git",
        "ssh://git@github.com:22/owner/repo",
        "git+ssh://git@github.com/owner/repo",
        "https://github.com/owner/repo?tab=readme#top",
    ],
)
def test_every_spelling_of_one_remote_has_one_canonical_form(raw: str) -> None:
    assert normalize_repository_url(raw) == CANONICAL


def test_equal_remotes_have_equal_keys_and_credentials_do_not_leak() -> None:
    a = derive_identity(repository_url="https://tok:secret@github.com/o/r.git", root_commits=None)
    b = derive_identity(repository_url="git@github.com:O/R", root_commits=None)
    assert a.key == b.key and a.kind == "remote_url"
    assert "secret" not in a.display and "tok" not in a.display
    assert len(a.key) == 64


def test_different_repositories_have_different_keys() -> None:
    a = derive_identity(repository_url="https://github.com/o/r1", root_commits=None)
    b = derive_identity(repository_url="https://github.com/o/r2", root_commits=None)
    c = derive_identity(repository_url="https://gitlab.com/o/r1", root_commits=None)
    assert len({a.key, b.key, c.key}) == 3


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "/Users/someone/code/repo",
        "~/code/repo",
        "./repo",
        "../repo",
        "C:\\Users\\x\\repo",
        "c:/Users/x/repo",
        "\\\\server\\share\\repo",
        "file:///Users/x/repo",
        "https://github.com/owner",  # no repository
        "https://github.com",
        "https://github.com/owner/../../etc/passwd",
        "https://github.com/owner/repo name",
        "https://github.com/owner/repo\x00",
        "ftp://github.com/owner/repo",
        "javascript:alert(1)",
        "https://nodot/owner/repo",
        "x" * 301,
    ],
)
def test_paths_and_garbage_are_refused(raw: str) -> None:
    with pytest.raises(ValidationError):
        normalize_repository_url(raw)


def test_root_commits_identity_is_order_and_case_insensitive() -> None:
    h1, h2 = "a" * 40, "B" * 40
    a = derive_identity(repository_url=None, root_commits=[h1, h2])
    b = derive_identity(repository_url=None, root_commits=[h2.lower(), h1, h1])
    assert a.key == b.key and a.kind == "root_commits"
    assert normalize_root_commits([h2, h1]) == sorted([h1, h2.lower()])


@pytest.mark.parametrize("bad", [[], ["short"], ["g" * 40], ["a" * 40] + ["b" * 41], [1]])
def test_bad_root_commits_are_refused(bad: list) -> None:
    with pytest.raises(ValidationError):
        normalize_root_commits(bad)


def test_too_many_root_commits_refused() -> None:
    with pytest.raises(ValidationError):
        normalize_root_commits([f"{i:040x}" for i in range(9)])


def test_a_remote_wins_over_root_commits_and_one_is_required() -> None:
    both = derive_identity(repository_url="https://github.com/o/r", root_commits=["a" * 40])
    assert both.kind == "remote_url"
    with pytest.raises(ValidationError):
        derive_identity(repository_url=None, root_commits=None)


def test_remote_and_root_namespaces_cannot_collide() -> None:
    # The same canonical text under the two derivations must never produce one key.
    a = derive_identity(repository_url="https://github.com/o/r", root_commits=None)
    b = derive_identity(repository_url=None, root_commits=["a" * 40])
    assert a.key != b.key


# -- tokens ----------------------------------------------------------------------------------


def test_project_and_session_tokens_are_not_interchangeable() -> None:
    project, session = new_project_token(), new_session_token()
    assert project.startswith(PROJECT_TOKEN_PREFIX) and is_project_token(project)
    assert is_well_formed_project_token(project) and not is_well_formed(project)
    assert is_well_formed(session) and not is_well_formed_project_token(session)
    assert not is_project_token(session)
    assert project != new_project_token()  # random


@pytest.mark.parametrize("bad", ["", "sbp_", "sbp_short", "sbp_" + "!" * 43, "sbp_" + "a" * 44])
def test_malformed_project_tokens_are_rejected_before_lookup(bad: str) -> None:
    assert not is_well_formed_project_token(bad)


def test_token_digest_is_the_only_stored_form() -> None:
    token = new_project_token()
    assert len(token_digest(token)) == 64 and token not in token_digest(token)
