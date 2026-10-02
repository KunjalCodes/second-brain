"""Git facts -> memory drafts (Phase 16, M4/M5): PURE and deterministic.

Git history and parsed structure are OBSERVED evidence, not claims to be weighed:
a commit hash is immutable, and a parser reports what the file contains. So they do
not go through duplicate/conflict classification; they are written with
deterministic ids (same fact -> same memory identity -> a replay cannot duplicate),
exact-identity entities, and a `git_commit` source.

Shape rules that keep the graph a representation of structure, not a copy of the
repository:

* one memory per commit (plus continuation parts of 20 files for large commits);
  per-file add/delete counts stay in PostgreSQL, not in the graph;
* one memory per file *structure* (symbols + imports), identified by a hash of that
  structure, so an unchanged file is never re-written and a changed one supersedes
  the old structure rather than leaving stale symbols "current";
* one memory per directory, once;
* no diffs, no file contents, no raw author emails.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.models.enums import EntityType
from app.projects.naming import (
    SHORT_SHA,
    branch_name,
    clean_text,
    commit_name,
    directory_name,
    file_name,
    symbol_name,
)

GIT_NAMESPACE = uuid.UUID("5b3a2f0e-6d54-4c0e-9a41-16a0e5b1c002")
COMMIT_PART_FILES = 20
STRUCTURE_PART_ITEMS = 40
MAX_BRANCHES_IN_COMMIT = 10
MAX_PARENTS = 8


def git_memory_id(space_id: str, *parts: str) -> str:
    """Deterministic, and space-specific (`Memory.id` is unique across spaces)."""
    return str(uuid.uuid5(GIT_NAMESPACE, ":".join([space_id, *parts])))


@dataclass(frozen=True)
class Draft:
    memory_id: str
    content: str
    entities: list[tuple[str, EntityType]]
    relationships: list[tuple[str, str, str]]
    #: `location` of the git_commit source (`commit` or `file:<path>`).
    location: str
    embed: bool = False
    #: Entity whose earlier ACTIVE memories with `supersede_prefix` this one replaces.
    supersede_entity: str | None = None
    supersede_prefix: str | None = None
    extra: dict = field(default_factory=dict)


def repository_entity(project_name: str) -> str:
    return "repository:" + clean_text(project_name, max_length=120, field="project name")


def project_entity(project_name: str) -> str:
    return "project:" + clean_text(project_name, max_length=120, field="project name")


def root_draft(space_id: str, project_name: str, commit_location: str = "commit") -> Draft:
    project, repo = project_entity(project_name), repository_entity(project_name)
    return Draft(
        memory_id=git_memory_id(space_id, "root"),
        content=f"Repository {repo.removeprefix('repository:')} belongs to project "
        f"{project.removeprefix('project:')}.",
        entities=[(project, EntityType.PROJECT), (repo, EntityType.REPOSITORY)],
        relationships=[(project, "contains", repo)],
        location=commit_location,
    )


# -- commits ---------------------------------------------------------------------------------


def _file_line(f: dict) -> str:
    stats = ""
    if f.get("additions") is not None or f.get("deletions") is not None:
        stats = f" (+{f.get('additions', '?')}/-{f.get('deletions', '?')})"
    old = f" <- {f['old_path']}" if f.get("old_path") else ""
    return f"{f['status']} {f['path']}{old}{stats}"


def commit_part_ids(space_id: str, commit_id: str, n_files: int) -> list[str]:
    parts = max(1, -(-n_files // COMMIT_PART_FILES))
    return [git_memory_id(space_id, "commit", commit_id, str(i)) for i in range(parts)]


def build_commit_drafts(
    *,
    space_id: str,
    commit_id: str,
    sha: str,
    parents: list[str],
    subject: str,
    committed_at: datetime,
    branches: list[str],
    files: list[dict],
    files_changed_count: int,
    files_truncated: bool,
) -> list[Draft]:
    commit = commit_name(sha)
    short = sha[:SHORT_SHA]
    chunks = [
        files[i : i + COMMIT_PART_FILES] for i in range(0, len(files), COMMIT_PART_FILES)
    ] or [[]]
    ids = commit_part_ids(space_id, commit_id, len(files))
    drafts: list[Draft] = []
    for index, chunk in enumerate(chunks):
        entities: list[tuple[str, EntityType]] = [(commit, EntityType.COMMIT)]
        rels: list[tuple[str, str, str]] = []
        if index == 0:
            content = f"Commit {short} ({committed_at.date().isoformat()}): {subject}"
            content += f" Changed {files_changed_count} file(s)"
            content += " (the list below is partial)." if files_truncated else "."
            on = branches[:MAX_BRANCHES_IN_COMMIT]
            if on:
                content += f" On branch(es): {', '.join(on)}."
            for b in on:
                entities.append((branch_name(b), EntityType.BRANCH))
                rels.append((commit, "on_branch", branch_name(b)))
            for parent in parents[:MAX_PARENTS]:
                entities.append((commit_name(parent), EntityType.COMMIT))
                rels.append((commit, "child_of", commit_name(parent)))
        else:
            content = f"Commit {short} (part {index + 1} of {len(chunks)}) changed files:"
        if chunk:
            content += "\n" + "\n".join(_file_line(f) for f in chunk)
        for f in chunk:
            name = file_name(f["path"])
            entities.append((name, EntityType.FILE))
            rels.append((commit, "changed", name))
        drafts.append(
            Draft(
                memory_id=ids[index],
                content=content[:9990],
                entities=_unique(entities),
                relationships=rels,
                location="commit",
                embed=index == 0,
            )
        )
    return drafts


def branch_draft(space_id: str, branch: str, tip: str) -> Draft:
    b, c = branch_name(branch), commit_name(tip)
    return Draft(
        memory_id=git_memory_id(space_id, "branch", branch, tip),
        content=f"Branch {branch} points at commit {tip[:SHORT_SHA]}.",
        entities=[(b, EntityType.BRANCH), (c, EntityType.COMMIT)],
        relationships=[(b, "points_at", c)],
        location="commit",
        supersede_entity=b,
        supersede_prefix=f"Branch {branch} points at",
    )


# -- structure -------------------------------------------------------------------------------


def structure_hash(file: dict) -> str:
    body = json.dumps(
        {
            "language": file["language"],
            "symbols": sorted((s["name"], s["kind"]) for s in file.get("symbols", [])),
            "imports": sorted(file.get("imports", [])),
            "dependencies": sorted(file.get("dependencies", [])),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def parent_directory(path: str) -> str | None:
    head, _, _ = path.rpartition("/")
    return head or None


def container_name(project_name: str, directory: str | None) -> tuple[str, EntityType]:
    if directory is None:
        return repository_entity(project_name), EntityType.REPOSITORY
    return directory_name(directory), EntityType.DIRECTORY


def structure_prefix(path: str) -> str:
    return f"Structure of file {path} ("


def build_file_drafts(*, space_id: str, project_name: str, file: dict) -> tuple[str, list[Draft]]:
    path = file["path"]
    digest = structure_hash(file)
    fname = file_name(path)
    holder, holder_type = container_name(project_name, parent_directory(path))
    items: list[tuple[str, EntityType, str]] = []
    for s in file.get("symbols", []):
        items.append((symbol_name(path, s["name"]), EntityType.CODE_SYMBOL, "defines"))
    for imported in file.get("imports", []):
        items.append((file_name(imported), EntityType.FILE, "depends_on"))
    for dep in file.get("dependencies", []):
        items.append(
            (
                f"dependency:{clean_text(dep, max_length=100, field='dependency')}",
                EntityType.DEPENDENCY,
                "depends_on",
            )
        )
    chunks = [
        items[i : i + STRUCTURE_PART_ITEMS] for i in range(0, len(items), STRUCTURE_PART_ITEMS)
    ] or [[]]
    drafts: list[Draft] = []
    n_class = sum(1 for s in file.get("symbols", []) if s["kind"] == "class")
    n_fn = sum(1 for s in file.get("symbols", []) if s["kind"] != "class")
    for index, chunk in enumerate(chunks):
        entities: list[tuple[str, EntityType]] = [(fname, EntityType.FILE)]
        rels: list[tuple[str, str, str]] = []
        if index == 0:
            entities.append((holder, holder_type))
            rels.append((holder, "contains", fname))
            content = (
                f"Structure of file {path} ({file['language']}): "
                f"{n_class} class(es), {n_fn} function(s)/method(s), "
                f"{len(file.get('imports', []))} internal import(s), "
                f"{len(file.get('dependencies', []))} external package(s)."
            )
            if file.get("symbols_truncated"):
                content += " Symbol list is partial."
        else:
            content = f"Structure of file {path} (part {index + 1} of {len(chunks)}):"
        for name, kind, predicate in chunk:
            entities.append((name, kind))
            rels.append((fname, predicate, name))
        if chunk:
            content += "\n" + "\n".join(f"{p} {n}" for n, _, p in chunk)
        drafts.append(
            Draft(
                memory_id=git_memory_id(space_id, "file", path, digest, str(index)),
                content=content[:9990],
                entities=_unique(entities),
                relationships=rels,
                location=f"file:{path}",
                supersede_entity=fname if index == 0 else None,
                supersede_prefix=structure_prefix(path) if index == 0 else None,
                extra={"path": path},
            )
        )
    return digest, drafts


def directory_draft(space_id: str, project_name: str, directory: str) -> Draft:
    parent, parent_type = container_name(project_name, parent_directory(directory))
    me = directory_name(directory)
    return Draft(
        memory_id=git_memory_id(space_id, "dir", directory),
        content=f"Directory {directory} is inside {parent}.",
        entities=[(parent, parent_type), (me, EntityType.DIRECTORY)],
        relationships=[(parent, "contains", me)],
        location="commit",
    )


def ancestors(path: str) -> list[str]:
    """Every ancestor directory of a file path, outermost first."""
    parts = path.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def _unique(items: list[tuple[str, EntityType]]) -> list[tuple[str, EntityType]]:
    seen: dict[str, EntityType] = {}
    for name, kind in items:
        seen.setdefault(name, kind)
    return list(seen.items())
