#!/usr/bin/env python3
"""Local READ-ONLY Git collector for the project-memory integration (Phase 16).

    python scripts/git_collect.py identity  --repo .
    python scripts/git_collect.py commits   --repo . --server http://localhost:8001
    python scripts/git_collect.py structure --repo . --server http://localhost:8001
    python scripts/git_collect.py all       --repo . --server ... --project current --dry-run

What it does: runs `git` (fixed argument lists, no shell) inside ONE local repository,
reads commit metadata, changed-file lists and -- for Python files only -- the
classes / functions / imports found by the standard-library `ast`, and POSTs bounded
JSON batches to the memory engine with a PROJECT TOKEN.

What it never does: write to the repository, send file contents or diffs, send raw
author emails (only a SHA-256 of the lower-cased address), send absolute paths, send
credentials embedded in a remote URL, or take the token from anywhere except an
environment variable (`--token-env`, default MEMORY_ENGINE_TOKEN). The server never
reads your disk; this script is the only thing that does, and only through `git`.

Standard library only. Re-running is safe: the server is idempotent by commit hash and
by structure hash.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# -- limits (mirrored by the server; the server enforces them regardless) ---------------------

MAX_PATH_LENGTH = 190
MAX_SEGMENT_LENGTH = 255
MAX_FILES_PER_COMMIT = 200
MAX_BODY_CHARS = 4000
MAX_SUBJECT_CHARS = 500
MAX_COMMITS_HARD = 5000
MAX_STRUCTURE_FILES_HARD = 20000
MAX_SYMBOLS_PER_FILE = 100
MAX_IMPORTS_PER_FILE = 100
MAX_DEPS_PER_FILE = 50
MAX_BRANCHES = 20
MAX_PY_BYTES = 200_000
COMMIT_BATCH = 25
STRUCTURE_BATCH = 100
GIT_TIMEOUT = 120

DEFAULT_EXCLUDES = (
    "node_modules/*", "*/node_modules/*", "vendor/*", "*/vendor/*", "dist/*", "build/*",
    "*.min.js", "*.lock", "package-lock.json", "*.map", "*.svg", "*.png", "*.jpg", "*.jpeg",
    "*.gif", "*.ico", "*.pdf", "*.zip", "*.gz", "*.woff", "*.woff2", "*.ttf", "*.pyc",
)  # fmt: skip

LANGUAGES = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".hpp": "cpp", ".cs": "csharp", ".swift": "swift", ".sh": "shell", ".sql": "sql",
    ".md": "markdown", ".json": "json", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".html": "html", ".css": "css", ".scss": "css", ".ini": "ini", ".cfg": "ini",
}  # fmt: skip

_CONTROL = re.compile(r"[\x00-\x1f\x7f  ‪-‮⁦-⁩]")
_SAFE_REV = re.compile(r"^[A-Za-z0-9._/@{}~^-]{1,100}$")
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BRANCH_BAD = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")


class CollectorError(Exception):
    pass


# -- git ---------------------------------------------------------------------------------------


def git(repo: str, *args: str, timeout: int = GIT_TIMEOUT, check: bool = True) -> bytes:
    """Run one READ-ONLY git command. Fixed argument lists, never a shell."""
    cmd = ["git", "-C", repo, "-c", "core.quotepath=false", "-c", "core.fsmonitor=false", *args]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8"}
    try:
        done = subprocess.run(  # noqa: S603 - fixed list, shell=False
            cmd, capture_output=True, timeout=timeout, env=env, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise CollectorError(f"git timed out: {args[0]}") from exc
    except FileNotFoundError as exc:
        raise CollectorError("git is not installed or not on PATH") from exc
    if check and done.returncode != 0:
        raise CollectorError(f"git {args[0]} failed: {done.stderr.decode(errors='replace')[:200]}")
    return done.stdout


def ensure_repo(repo: str) -> str:
    if not os.path.isdir(repo):
        raise CollectorError("--repo must be a directory")
    inside = git(repo, "rev-parse", "--is-inside-work-tree", check=False).strip()
    if inside != b"true":
        raise CollectorError("--repo is not inside a git work tree")
    return repo


def safe_rev(value: str) -> str:
    if value.startswith("-") or not _SAFE_REV.match(value):
        raise CollectorError("refusing an unsafe revision argument")
    return value


# -- identity ------------------------------------------------------------------------------------


def strip_credentials(url: str) -> str:
    """Drop any `user:password@` from a URL before it leaves this machine."""
    if "://" not in url:
        return re.sub(r"^[^@/\s]+@", "git@", url) if "@" in url.split(":")[0] else url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def repository_identity(repo: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    url = git(repo, "config", "--get", "remote.origin.url", check=False).decode().strip()
    if not url:
        remotes = git(repo, "remote", check=False).decode().split()
        if remotes:
            url = (
                git(repo, "config", "--get", f"remote.{remotes[0]}.url", check=False)
                .decode()
                .strip()
            )
    if url and not url.startswith(("/", ".", "~", "file:")):
        out["repository_url"] = strip_credentials(url)[:300]
    roots = git(repo, "rev-list", "--max-parents=0", "HEAD", check=False).decode().split()
    roots = [r for r in roots if _SHA.match(r)][:8]
    if roots:
        out["root_commits"] = roots
    if not out:
        raise CollectorError("repository has neither a remote URL nor any commits")
    return out


# -- path rules (a copy of the server's; tests assert they agree) ---------------------------------


def normalize_path(raw: str) -> str | None:
    """A safe repository-relative POSIX path, or None if it cannot be represented."""
    value = unicodedata.normalize("NFC", raw.strip())
    if not value or len(value) > MAX_PATH_LENGTH or _CONTROL.search(value) or "\\" in value:
        return None
    if value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", value):
        return None
    segments = [s for s in value.split("/") if s not in ("", ".")]
    if not segments or any(s == ".." or len(s) > MAX_SEGMENT_LENGTH for s in segments):
        return None
    if segments[0] == ".git":
        return None
    return "/".join(segments)


def clean_text(raw: str, limit: int) -> str:
    value = unicodedata.normalize("NFC", raw).replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f  ‪-‮⁦-⁩]", "", value)
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n"))
    return "\n".join(line for line in lines if line)[:limit].rstrip()


# -- commits -------------------------------------------------------------------------------------

_RS, _US = "\x1e", "\x1f"


def collect_branches(repo: str) -> list[dict[str, str]]:
    rows = git(repo, "for-each-ref", "--format=%(refname:short)\t%(objectname)", "refs/heads")
    out = []
    for line in rows.decode(errors="replace").splitlines():
        name, _, tip = line.partition("\t")
        if (
            name
            and _SHA.match(tip)
            and not _BRANCH_BAD.search(name)
            and not name.endswith(("/", "."))
        ):
            out.append({"name": name, "tip": tip})
    return out[:MAX_BRANCHES]


def _branch_membership(
    repo: str, branches: list[dict[str, str]], limit: int
) -> dict[str, list[str]]:
    member: dict[str, list[str]] = {}
    for b in branches:
        revs = git(repo, "rev-list", f"--max-count={limit}", b["tip"], check=False).decode().split()
        for sha in revs:
            member.setdefault(sha, []).append(b["name"])
    return member


def parse_name_status(text: str) -> dict[str, list[dict[str, Any]]]:
    """`git log --name-status` (one `\\x1e<sha>` header per commit) -> {sha: [file, ...]}."""
    result: dict[str, list[dict[str, Any]]] = {}
    for block in text.split(_RS):
        if not block.strip():
            continue
        head, _, rest = block.partition("\n")
        sha = head.strip()
        files = []
        for line in rest.splitlines():
            if not line.strip():
                continue
            cols = line.split("\t")
            code = cols[0][:1]
            if code in "AMDT" and len(cols) == 2:
                files.append({"status": code, "path": cols[1]})
            elif code in "RC" and len(cols) == 3:
                files.append({"status": code, "old_path": cols[1], "path": cols[2]})
        result[sha] = files
    return result


def parse_numstat(text: str) -> dict[str, dict[str, tuple[int | None, int | None]]]:
    result: dict[str, dict[str, tuple[int | None, int | None]]] = {}
    for block in text.split(_RS):
        if not block.strip():
            continue
        head, _, rest = block.partition("\n")
        stats: dict[str, tuple[int | None, int | None]] = {}
        for line in rest.splitlines():
            cols = line.split("\t")
            if len(cols) != 3 or "=>" in cols[2]:
                continue  # renames are reported as `a => b`; their counts are skipped
            add = int(cols[0]) if cols[0].isdigit() else None
            dele = int(cols[1]) if cols[1].isdigit() else None
            stats[cols[2]] = (add, dele)
        result[head.strip()] = stats
    return result


def collect_commits(
    repo: str, max_commits: int, since: str | None = None
) -> tuple[list[dict], list[dict]]:
    max_commits = max(1, min(max_commits, MAX_COMMITS_HARD))
    rev = [safe_rev(since) + "..HEAD"] if since else ["HEAD"]
    fmt = f"--format={_RS}%H{_US}%P{_US}%an{_US}%ae{_US}%aI{_US}%cI{_US}%s{_US}%b"
    meta = git(
        repo, "log", "--no-color", "--no-ext-diff", f"--max-count={max_commits}", fmt, *rev, "--"
    )
    names = git(repo, "log", "--no-color", "--no-ext-diff", "-M", "--name-status",
                f"--max-count={max_commits}", f"--format={_RS}%H", *rev, "--")  # fmt: skip
    nums = git(repo, "log", "--no-color", "--no-ext-diff", "-M", "--numstat",
               f"--max-count={max_commits}", f"--format={_RS}%H", *rev, "--")  # fmt: skip
    files_by = parse_name_status(names.decode(errors="replace"))
    stats_by = parse_numstat(nums.decode(errors="replace"))
    branches = collect_branches(repo)
    membership = _branch_membership(repo, branches, max_commits)
    commits: list[dict] = []
    for record in meta.decode(errors="replace").split(_RS):
        if not record.strip():
            continue
        fields = record.split(_US)
        if len(fields) < 8 or not _SHA.match(fields[0].strip()):
            continue  # a malformed record is skipped, never guessed at
        sha, parents, an, ae, ad, cd, subject = (f.strip() for f in fields[:7])
        body = _US.join(fields[7:]).strip()
        raw_files = files_by.get(sha, [])
        listed: list[dict[str, Any]] = []
        total_add = total_del = 0
        for f in raw_files:
            path = normalize_path(f["path"])
            old = normalize_path(f["old_path"]) if f.get("old_path") else None
            add, dele = stats_by.get(sha, {}).get(f["path"], (None, None))
            total_add += add or 0
            total_del += dele or 0
            if (
                path is None
                or (f.get("old_path") and old is None)
                or len(listed) >= MAX_FILES_PER_COMMIT
            ):
                continue
            item: dict[str, Any] = {"path": path, "status": f["status"]}
            if old:
                item["old_path"] = old
            if add is not None:
                item["additions"] = add
            if dele is not None:
                item["deletions"] = dele
            listed.append(item)
        body_clean = clean_text(body, MAX_BODY_CHARS + 1)
        commit: dict[str, Any] = {
            "sha": sha,
            "parents": [p for p in parents.split() if _SHA.match(p)][:8],
            "committed_at": cd,
            "subject": clean_text(subject, MAX_SUBJECT_CHARS) or "(no message)",
            "branches": membership.get(sha, [])[:20],
            "files": listed,
            "files_changed_count": len(raw_files),
            "files_truncated": len(listed) < len(raw_files),
            "additions": total_add if raw_files else None,
            "deletions": total_del if raw_files else None,
        }
        if an.strip():
            commit["author_name"] = clean_text(an, 200)
        if ae.strip():
            commit["author_email_sha256"] = hashlib.sha256(ae.strip().lower().encode()).hexdigest()
        if ad.strip():
            commit["authored_at"] = ad
        if body_clean:
            commit["body"] = body_clean[:MAX_BODY_CHARS]
            commit["body_truncated"] = len(body_clean) > MAX_BODY_CHARS
        commits.append(commit)
    return commits, branches


# -- structure -----------------------------------------------------------------------------------


def language_of(path: str) -> str:
    return LANGUAGES.get(PurePosixPath(path).suffix.lower(), "other")


def excluded(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def list_files(
    repo: str, rev: str, patterns: Iterable[str], max_files: int
) -> tuple[list[tuple[str, int]], int]:
    """(path, size) of tracked files at `rev`, plus how many were skipped."""
    raw = git(repo, "ls-tree", "-r", "-l", "-z", safe_rev(rev))
    out: list[tuple[str, int]] = []
    skipped = 0
    for entry in raw.split(b"\x00"):
        if not entry:
            continue
        meta, _, path_b = entry.partition(b"\t")
        parts = meta.split()
        if len(parts) < 4 or parts[1] != b"blob":
            continue  # submodules (commit) and trees are not files
        path = normalize_path(path_b.decode(errors="replace"))
        if path is None or excluded(path, patterns) or len(out) >= max_files:
            skipped += 1
            continue
        size = int(parts[3]) if parts[3].isdigit() else 0
        out.append((path, size))
    return out, skipped


def read_blobs(repo: str, rev: str, paths: list[str]) -> dict[str, bytes]:
    """Contents of the given paths at `rev` through ONE `git cat-file --batch`."""
    if not paths:
        return {}
    proc = subprocess.Popen(  # noqa: S603
        ["git", "-C", repo, "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )  # fmt: skip
    assert proc.stdin and proc.stdout
    out: dict[str, bytes] = {}
    try:
        for path in paths:
            proc.stdin.write(f"{rev}:{path}\n".encode())
            proc.stdin.flush()
            header = proc.stdout.readline().decode(errors="replace").split()
            if len(header) == 3 and header[1] == "blob":
                size = int(header[2])
                out[path] = proc.stdout.read(size)
                proc.stdout.read(1)  # trailing newline
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    return out


def module_index(py_paths: list[str]) -> dict[str, set[str]]:
    """Every dotted suffix of every Python module path -> the files it could name."""
    index: dict[str, set[str]] = {}
    for p in py_paths:
        stem = p[:-3]
        if stem.endswith("/__init__"):
            stem = stem[: -len("/__init__")]
        parts = [s for s in stem.split("/") if s]
        for i in range(len(parts)):
            index.setdefault(".".join(parts[i:]), set()).add(p)
    return index


def _stdlib() -> frozenset[str]:
    return frozenset(getattr(sys, "stdlib_module_names", ()))


def analyse_python(
    path: str,
    source: bytes,
    index: dict[str, set[str]],
    local_names: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Classes, functions, methods and imports via the stdlib `ast`. A file that does
    not parse yields no symbols (never a guess)."""
    result: dict[str, Any] = {
        "symbols": [],
        "imports": [],
        "dependencies": [],
        "symbols_truncated": False,
    }
    try:
        tree = ast.parse(source.decode("utf-8", errors="replace"), filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return result
    symbols: list[dict[str, Any]] = []

    def walk(body: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                symbols.append({"name": prefix + node.name, "kind": "class", "line": node.lineno})
                walk(node.body, f"{prefix}{node.name}.", True)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if in_class else "function"
                symbols.append({"name": prefix + node.name, "kind": kind, "line": node.lineno})

    walk(tree.body, "", False)
    if len(symbols) > MAX_SYMBOLS_PER_FILE:
        result["symbols_truncated"] = True
    result["symbols"] = [s for s in symbols if len(s["name"]) <= 100][:MAX_SYMBOLS_PER_FILE]

    here = PurePosixPath(path).parent
    imports: list[str] = []
    deps: list[str] = []
    std = _stdlib()

    def resolve(dotted: str) -> str | None:
        hits = index.get(dotted)
        return next(iter(hits)) if hits and len(hits) == 1 else None

    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: anchored at this file's package directory
                base = here
                for _ in range(node.level - 1):
                    base = base.parent
                module = ".".join([*base.parts, *(node.module.split(".") if node.module else [])])
                names = [module] + [f"{module}.{a.name}" for a in node.names]
            elif node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        for dotted in names:
            target = resolve(dotted)
            if target and target != path:
                imports.append(target)
            elif not node.__dict__.get("level") and dotted == names[0]:
                top = dotted.split(".")[0]
                if (
                    top
                    and top not in std
                    and top != "__future__"
                    and not resolve(top)
                    and top not in index
                    and top not in local_names
                ):
                    deps.append(top)
    result["imports"] = sorted(set(imports))[:MAX_IMPORTS_PER_FILE]
    result["dependencies"] = sorted(
        set(d for d in deps if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$", d))
    )[:MAX_DEPS_PER_FILE]
    return result


def collect_structure(
    repo: str, rev: str, patterns: Iterable[str], max_files: int
) -> tuple[str, list[dict[str, Any]], int]:
    """(commit sha of `rev`, per-file structure, files skipped)."""
    sha = git(repo, "rev-parse", "--verify", safe_rev(rev) + "^{commit}").decode().strip()
    files, skipped = list_files(repo, sha, patterns, min(max_files, MAX_STRUCTURE_FILES_HARD))
    py = [p for p, size in files if p.endswith(".py") and size <= MAX_PY_BYTES]
    all_py = [p for p, _ in files if p.endswith(".py")]
    index = module_index(all_py)
    local_names = frozenset(part for p in all_py for part in p[:-3].split("/"))
    blobs = read_blobs(repo, sha, py)
    out: list[dict[str, Any]] = []
    for path, size in files:
        entry: dict[str, Any] = {"path": path, "language": language_of(path), "size_bytes": size}
        if path in blobs:
            analysis = analyse_python(path, blobs[path], index, local_names)
            entry.update({k: v for k, v in analysis.items() if v or k == "symbols_truncated"})
        out.append(entry)
    return sha, out, skipped


# -- transport -----------------------------------------------------------------------------------


def post(server: str, token: str, path: str, body: dict, retries: int = 3) -> dict:
    """POST JSON with a bearer token. Retries only what is safe to retry: connection
    errors and 5xx (every endpoint is idempotent). The token is never printed."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(  # noqa: S310 - scheme checked in main
        server.rstrip("/") + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            if exc.code >= 500 and attempt < retries:
                time.sleep(2**attempt)
                continue
            raise CollectorError(f"server answered {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries:
                time.sleep(2**attempt)
                continue
            raise CollectorError(
                f"could not reach the server: {getattr(exc, 'reason', exc)}"
            ) from None
    raise CollectorError("unreachable")  # pragma: no cover


def chunked(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# -- commands ------------------------------------------------------------------------------------


def run_commits(args: argparse.Namespace, token: str | None) -> None:
    identity = repository_identity(args.repo)
    commits, branches = collect_commits(args.repo, args.max_commits, args.since)
    commits.reverse()  # oldest first: parents are recorded before their children
    print(f"collected {len(commits)} commit(s), {len(branches)} branch(es)")
    if args.dry_run or not token:
        return
    totals = {"recorded": 0, "duplicate": 0}
    pending = 0
    for batch in chunked(commits, COMMIT_BATCH):
        body = {"identity": identity, "branches": branches, "commits": batch}
        result = post(args.server, token, f"/projects/{args.project}/git/commits", body)
        for r in result["results"]:
            totals[r["status"]] += 1
            pending += r["projection_status"] not in ("projected", "skipped")
    print(
        f"commits: {totals['recorded']} recorded, {totals['duplicate']} already known, "
        f"{pending} awaiting projection"
    )


def run_structure(args: argparse.Namespace, token: str | None) -> None:
    identity = repository_identity(args.repo)
    patterns = [*DEFAULT_EXCLUDES, *args.exclude]
    sha, files, skipped = collect_structure(args.repo, args.rev, patterns, args.max_files)
    print(f"structure of {sha[:12]}: {len(files)} file(s), {skipped} skipped")
    if args.dry_run or not token:
        return
    created = existing = superseded = stale = 0
    for batch in chunked(files, STRUCTURE_BATCH):
        body = {"identity": identity, "commit_sha": sha, "files": batch}
        r = post(args.server, token, f"/projects/{args.project}/git/structure", body)
        created += r["memories_created"]
        existing += r["memories_existing"]
        superseded += r["memories_superseded"]
        stale += r["files_stale"]
    print(
        f"structure: {created} created, {existing} unchanged, "
        f"{superseded} superseded, {stale} stale"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("command", choices=["identity", "commits", "structure", "all"])
    p.add_argument("--repo", default=".")
    p.add_argument("--server", default="http://localhost:8001")
    p.add_argument(
        "--project", default="current", help="project id, or 'current' for a project token"
    )
    p.add_argument("--token-env", default="MEMORY_ENGINE_TOKEN")
    p.add_argument("--max-commits", type=int, default=200)
    p.add_argument("--since", help="only commits after this revision (e.g. a tag or hash)")
    p.add_argument("--rev", default="HEAD", help="revision to read the structure from")
    p.add_argument("--max-files", type=int, default=5000)
    p.add_argument("--exclude", action="append", default=[], help="extra glob to skip (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="read and summarise; send nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ensure_repo(args.repo)
        if args.command == "identity":
            print(json.dumps(repository_identity(args.repo), indent=2))
            return 0
        token = os.environ.get(args.token_env)
        if not args.dry_run:
            if not token:
                raise CollectorError(f"set {args.token_env} (a project token) or use --dry-run")
            if urlsplit(args.server).scheme not in ("http", "https"):
                raise CollectorError("--server must be an http(s) URL")
            if args.project != "current" and not re.match(r"^[0-9a-fA-F-]{36}$", args.project):
                raise CollectorError("--project must be a project id or 'current'")
        if args.command in ("commits", "all"):
            run_commits(args, token)
        if args.command in ("structure", "all"):
            run_structure(args, token)
    except CollectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
