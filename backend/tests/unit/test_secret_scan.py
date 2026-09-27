"""Phase 11 (M6): the offline secret scanner (scripts/secret_scan.py).

Sample secrets are ASSEMBLED at runtime (never written literally) so this
file does not itself trip the scanner.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from secret_scan import scan  # noqa: E402

SAMPLES = {
    "google_api_key": "AI" + "za" + "S" * 35,
    "openai_key": "sk-" + "a1B2" * 10,
    "anthropic_key": "sk-" + "ant-" + "x" * 40,
    "aws_access_key": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
    "private_key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "session_token": "sb" + "s_" + "t" * 43,
    "credential_url": "postgresql" + "://admin:hunter2hunter2@db.example.com/prod",
}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_each_secret_shape_is_found(tmp_path: Path, name: str) -> None:
    (tmp_path / "f.py").write_text(f'value = "{SAMPLES[name]}"\n')
    findings = scan(["f.py"], root=tmp_path)
    assert findings == [f"f.py:1: possible {name}"]
    assert SAMPLES[name] not in findings[0], "findings never print the value"


@pytest.mark.parametrize(
    "safe",
    [
        "postgresql" + "://user:pw@host/db",  # docs placeholder (short)
        "postgresql" + "://u:<password>@host/db",
        "neo4j+s" + "://neo4j:${NEO4J_PASSWORD}@x.databases.neo4j.io",
        'PASSWORD = "correct horse battery staple"',
    ],
)
def test_placeholders_are_not_flagged(tmp_path: Path, safe: str) -> None:
    (tmp_path / "f.md").write_text(safe + "\n")
    assert scan(["f.md"], root=tmp_path) == []


@pytest.mark.parametrize(
    "name",
    [".env", "backend/.env", ".env.production", "server.pem", "db.dump", "backups/pg.sql.gz"],
)
def test_forbidden_files_are_flagged_whatever_their_content(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("harmless\n")
    assert scan([name], root=tmp_path) == [f"{name}: file type must not be committed"]


def test_env_example_is_allowed(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("GEMINI_API_KEY=\n")
    assert scan([".env.example"], root=tmp_path) == []
