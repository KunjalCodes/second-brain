"""Phase 16 naming and input hygiene: paths are safe, names are exact."""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.extraction.resolution import EXACT_IDENTITY_TYPES
from app.models.enums import PROJECT_PREDICATES, EntityType, SourceType
from app.models.normalization import normalize_name
from app.projects.naming import (
    branch_name,
    clean_text,
    commit_name,
    directory_name,
    file_name,
    normalize_branch,
    normalize_path,
    normalize_session_key,
    normalize_sha,
    session_name,
    symbol_name,
    titled_name,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/auth/session.py", "src/auth/session.py"),
        ("./src//auth/./session.py", "src/auth/session.py"),
        ("  README.md  ", "README.md"),
        ("src/ünï/файл.py", "src/ünï/файл.py"),
        ("a/.hidden/x", "a/.hidden/x"),
    ],
)
def test_paths_are_normalised_to_a_safe_relative_form(raw: str, expected: str) -> None:
    assert normalize_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        ".",
        "./",
        "/etc/passwd",
        "~/x",
        "C:/Windows/system32",
        "c:foo",
        "../secret",
        "a/../../b",
        "a/..",
        "a\\b",
        "src/x\x00.py",
        "src/x\n.py",
        ".git/config",
        ".git",
        "a" * 191,
        "a/" + "b" * 256,
        "x\u202ey.py",  # bidi override: a classic filename spoof
    ],
)
def test_hostile_paths_are_refused(raw: str) -> None:
    with pytest.raises(ValidationError):
        normalize_path(raw)


def test_a_path_never_normalises_into_a_traversal() -> None:
    # Whatever survives normalisation must be free of `..` and absolute prefixes.
    for raw in ["a/b/c", "./a/./b", "a//b", "..a/b", "a../b", "...", ".../x"]:
        out = normalize_path(raw)
        assert ".." not in out.split("/") and not out.startswith("/")


def test_structural_names_are_exact_and_prefixed() -> None:
    sha = "ab" * 20
    assert file_name("src/a.py") == "file:src/a.py"
    assert directory_name("src") == "dir:src"
    assert commit_name(sha) == "commit:" + sha[:12]
    assert session_name("s-1") == "session:s-1"
    assert branch_name("feature/auth") == "branch:feature/auth"
    assert symbol_name("a.py", "Auth.login") == "symbol:a.py::Auth.login"


def test_look_alike_files_do_not_share_identity() -> None:
    # `auth/session.py` and `auth_session.py` canonicalise identically for FUZZY
    # matching, which is exactly why exact-identity types are exempt from it.
    a, b = file_name("auth/session.py"), file_name("auth_session.py")
    assert normalize_name(a) != normalize_name(b)
    assert EntityType.FILE in EXACT_IDENTITY_TYPES


def test_titled_names_make_the_same_title_one_entity_across_types() -> None:
    assert normalize_name(titled_name("bug", "Session  timeout")) == normalize_name(
        titled_name("bug", "session timeout")
    )
    # ...and a bug and a decision with the same words never collide.
    assert normalize_name(titled_name("bug", "x y")) != normalize_name(
        titled_name("decision", "x y")
    )


@pytest.mark.parametrize("bad", ["", "abc", "g" * 40, "A" * 39, "a" * 41, "a" * 40 + "\n"])
def test_bad_shas_are_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        normalize_sha(bad)


def test_sha_is_lowercased() -> None:
    assert normalize_sha("A" * 40) == "a" * 40


@pytest.mark.parametrize("ok", ["s1", "run-2026-10-01.a", "claude:abc_123", "A" * 64])
def test_session_keys_accepted(ok: str) -> None:
    assert normalize_session_key(ok) == ok


@pytest.mark.parametrize("bad", ["", " ", "-x", ".x", "a b", "a/b", "a" * 65, "x\n", "é"])
def test_session_keys_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        normalize_session_key(bad)


@pytest.mark.parametrize(
    "bad",
    ["", "a..b", "a b", "a~1", "a^", "a:b", "a?", "a*", "a[", "a\\b", "x/", "x.", "@{", "a//b"],
)
def test_bad_branches_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        normalize_branch(bad)


def test_text_is_cleaned_and_bounded_never_silently_truncated() -> None:
    assert clean_text("  a \t b\r\n\n\n c \x00\x07", max_length=50) == "a b\nc"
    assert clean_text("evil\u202etxt", max_length=50) == "eviltxt"
    with pytest.raises(ValidationError):
        clean_text("x" * 11, max_length=10)
    with pytest.raises(ValidationError):
        clean_text(" \x00 ", max_length=10)
    with pytest.raises(ValidationError):
        clean_text(5, max_length=10)  # type: ignore[arg-type]


def test_phase16_vocabulary_is_additive() -> None:
    assert EntityType("file") is EntityType.FILE and EntityType("person") is EntityType.PERSON
    assert SourceType("git_commit") is SourceType.GIT_COMMIT
    assert SourceType("agent_event") is SourceType.AGENT_EVENT
    assert {"modified", "fixed_by", "led_to", "superseded_by"} <= PROJECT_PREDICATES
    # Extraction (LLM) prompts must not be taught the structural types.
    from pathlib import Path

    from app.llm.prompts import extraction_v1

    source = Path(extraction_v1.__file__).read_text()
    assert "code_symbol" not in source and "agent_session" not in source
