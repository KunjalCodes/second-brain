"""Offline secret scan of the git index (Phase 11, M6/M11).

    python scripts/secret_scan.py            # scan tracked + staged files
    python scripts/secret_scan.py --staged   # only files staged for commit

Zero dependencies and no network, so it runs identically in CI and before a
local commit. It looks for HIGH-CONFIDENCE credential shapes (provider API
keys, private keys, session tokens, connection strings with an embedded
password) and for files that must never be committed (.env files, database
dumps and backup archives, key material). Exit status 1 on any finding.

It is a safety net, not a guarantee: a secret in an unusual format will not
be recognized. Findings print the file, line and pattern name -- never the
matched value.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PATTERNS: dict[str, re.Pattern[str]] = {
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "openai_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{32,}"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    "session_token": re.compile(r"\bsbs_[A-Za-z0-9_\-]{40,}"),
    # scheme://user:password@host with a real-looking password (placeholders
    # such as <pw>, ${VAR}, ***, or very short samples are ignored).
    "credential_url": re.compile(
        r"\b(?:postgres(?:ql)?(?:\+asyncpg)?|neo4j(?:\+s|\+ssc)?|bolt|redis|mongodb(?:\+srv)?)"
        r"://[^:\s/@]+:(?![<$*{])[^@\s'\"<>]{8,}@"
    ),
}

#: Files that must never be tracked, whatever they contain.
FORBIDDEN_NAMES = re.compile(
    r"(^|/)\.env(\.[^/]*)?$|\.pem$|\.key$|\.p12$|\.pfx$|id_rsa|\.dump$|\.pgdump$|"
    r"\.sql\.gz$|\.backup$|(^|/)backups?/|\.graph\.jsonl(\.gz)?(\.enc)?$"
)
#: Tracked files that legitimately match FORBIDDEN_NAMES.
ALLOWED_NAMES = re.compile(r"(^|/)\.env\.example$")

#: Skip binary/vendored content.
SKIP = re.compile(r"(^|/)(node_modules|\.next|\.venv|uv\.lock|package-lock\.json)(/|$)")


def _git_files(staged: bool) -> list[str]:
    cmd = (
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
        if staged
        else ["git", "ls-files", "--cached"]
    )
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def scan(paths: list[str], *, root: Path) -> list[str]:
    findings: list[str] = []
    for rel in paths:
        if SKIP.search(rel):
            continue
        if FORBIDDEN_NAMES.search(rel) and not ALLOWED_NAMES.search(rel):
            findings.append(f"{rel}: file type must not be committed")
            continue
        path = root / rel
        try:
            raw = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue
        if b"\x00" in raw[:4096]:
            continue  # binary
        text = raw.decode("utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "secret-scan: allow" in line:
                continue
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{rel}:{lineno}: possible {name}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    paths = _git_files(args.staged)
    findings = scan(paths, root=root)
    for finding in findings:
        print(finding)
    print(f"secret scan: {len(paths)} files, {len(findings)} finding(s)", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
