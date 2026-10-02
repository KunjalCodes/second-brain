"""Phase 11 (M8): every API route has a test that calls it with data.

`CONTRACT_TESTS` maps each route to the test file that exercises its
populated response. The test fails when:

* a route exists that is not in the map (a new route shipped without a
  contract test -- the class of gap behind the Phase 10 `POST /search` 500);
* a mapped file does not actually call that route (the map went stale).

Adding a route means adding its contract test AND a line here.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute

from app.main import create_app

TESTS = Path(__file__).resolve().parents[1]

CONTRACT_TESTS: dict[str, str] = {
    "POST /auth/login": "integration/test_auth.py",
    "POST /auth/logout": "integration/test_auth.py",
    "POST /auth/logout-all": "integration/test_auth.py",
    "GET /auth/me": "integration/test_auth.py",
    "POST /auth/password": "integration/test_auth.py",
    "POST /auth/register": "integration/test_auth.py",
    "GET /conversations": "integration/test_conversations_api.py",
    "POST /conversations": "integration/test_conversations_api.py",
    "GET /conversations/{conversation_id}": "integration/test_agent_api.py",
    "POST /conversations/{conversation_id}/agent-turn": "integration/test_agent_api.py",
    "GET /conversations/{conversation_id}/messages": "integration/test_conversations_api.py",
    "POST /conversations/{conversation_id}/messages": "integration/test_conversations_api.py",
    "GET /documents": "integration/test_documents_api.py",
    "POST /documents": "integration/test_upload_limits.py",
    "GET /documents/{document_id}": "integration/test_documents_api.py",
    "GET /documents/{document_id}/chunks": "integration/test_documents_api.py",
    "GET /entities/by-name": "unit/test_api.py",
    "GET /entities/search": "integration/test_graph_api.py",
    "GET /entities/{entity_id}": "unit/test_api.py",
    "GET /entities/{entity_id}/graph": "integration/test_graph_api.py",
    "GET /entities/{entity_id}/history": "unit/test_api.py",
    "GET /entities/{entity_id}/memories": "unit/test_api.py",
    "GET /entities/{entity_id}/relationships": "unit/test_api.py",
    "GET /projects": "integration/test_projects.py",
    "POST /projects": "integration/test_projects.py",
    "POST /projects/{project_id}/events": "integration/test_project_events.py",
    "POST /projects/{project_id}/events/project": "integration/test_project_events.py",
    "POST /projects/{project_id}/git/commits": "integration/test_project_git.py",
    "POST /projects/{project_id}/git/structure": "integration/test_project_git.py",
    "GET /projects/{project_id}/memory/search": "integration/test_project_memory.py",
    "GET /projects/{project_id}/entity": "integration/test_project_memory.py",
    "GET /projects/{project_id}/graph": "integration/test_project_memory.py",
    "GET /projects/{project_id}/history": "integration/test_project_memory.py",
    "GET /projects/{project_id}/files/history": "integration/test_project_memory.py",
    "GET /projects/{project_id}/commits/{sha}": "integration/test_project_memory.py",
    "GET /projects/{project_id}/related-changes": "integration/test_project_memory.py",
    "GET /projects/{project_id}/decisions": "integration/test_project_memory.py",
    "GET /projects/{project_id}/bugs": "integration/test_project_memory.py",
    "GET /projects/{project_id}/errors": "integration/test_project_memory.py",
    "GET /projects/{project_id}/fixes": "integration/test_project_memory.py",
    "GET /projects/{project_id}/attempts": "integration/test_project_memory.py",
    "GET /projects/{project_id}/sessions": "integration/test_project_memory.py",
    "GET /projects/{project_id}/sessions/{session_id}": "integration/test_project_memory.py",
    "GET /projects/{project_id}/sources/memory/{memory_id}": "integration/test_project_memory.py",
    "GET /projects/{project_id}/sources/event/{event_id}": "integration/test_project_memory.py",
    "GET /projects/{project_id}/sources/commit/{sha}": "integration/test_project_memory.py",
    "POST /projects/{project_id}/memory/propose": "integration/test_project_memory.py",
    "GET /projects/{project_id}/resume": "integration/test_project_resume.py",
    "GET /projects/{project_id}": "integration/test_projects.py",
    "GET /projects/{project_id}/tokens": "integration/test_projects.py",
    "POST /projects/{project_id}/tokens": "integration/test_projects.py",
    "POST /projects/{project_id}/tokens/{token_id}/revoke": "integration/test_projects.py",
    "GET /extraction/jobs": "integration/test_contracts.py",
    "GET /extraction/jobs/{job_id}": "integration/test_contracts.py",
    "GET /graph/edges/evidence": "integration/test_graph_api.py",
    "GET /graph/entities/{entity_id}/neighborhood": "integration/test_graph_api.py",
    "GET /graph/memories/{memory_id}/timeline": "integration/test_graph_api.py",
    "GET /graph/overview": "integration/test_graph_api.py",
    "GET /health": "unit/test_error_leaks.py",
    "GET /health/ready": "integration/test_readiness.py",
    "POST /internal/embeddings/backfill": "integration/test_contracts.py",
    "POST /internal/extraction/sweep": "integration/test_contracts.py",
    "GET /internal/metrics": "unit/test_observability.py",
    "POST /internal/review/reconcile": "integration/test_contracts.py",
    "POST /memories": "unit/test_api.py",
    "GET /memories/search": "unit/test_api.py",
    "GET /memories/{memory_id}": "unit/test_api.py",
    "PATCH /memories/{memory_id}": "unit/test_api.py",
    "POST /memories/{memory_id}/invalidate": "unit/test_api.py",
    "GET /memories/{memory_id}/provenance": "integration/test_auth.py",
    "GET /memories/{memory_id}/explanation": "integration/test_memory_intelligence.py",
    "GET /memories/{memory_id}/evolution": "integration/test_graph_intelligence.py",
    "GET /entities/{entity_id}/context": "integration/test_graph_intelligence.py",
    "GET /graph/paths": "integration/test_graph_intelligence.py",
    "GET /graph/edges/explain": "integration/test_graph_intelligence.py",
    "POST /graph/context": "integration/test_graph_intelligence.py",
    "GET /review/entities": "integration/test_contracts.py",
    "GET /review/entities/{candidate_id}": "integration/test_contracts.py",
    "POST /review/entities/{candidate_id}/approve-merge": "integration/test_contracts.py",
    "POST /review/entities/{candidate_id}/reject-merge": "integration/test_contracts.py",
    "POST /review/entities/{candidate_id}/reverse-merge": "integration/test_contracts.py",
    "GET /review/history": "integration/test_contracts.py",
    "GET /review/memories": "integration/test_contracts.py",
    "GET /review/memories/{candidate_id}": "integration/test_contracts.py",
    "POST /review/memories/{candidate_id}/confirm-distinct": "integration/test_contracts.py",
    "POST /review/memories/{candidate_id}/confirm-duplicate": "integration/test_contracts.py",
    "POST /review/memories/{candidate_id}/resolve-conflict": "integration/test_contracts.py",
    "POST /search": "unit/test_search_route.py",
}


def _routes() -> set[str]:
    found = set()
    for route in create_app().routes:
        items = (
            [route]
            if isinstance(route, APIRoute)
            else (
                route.effective_route_contexts()
                if hasattr(route, "effective_route_contexts")
                else []
            )
        )
        for item in items:
            for method in item.methods:
                found.add(f"{method} {item.path}")
    return found


def _calls(source: str, method: str, path: str) -> bool:
    parts = re.split(r"\{[^}]+\}", path)
    pattern = re.compile(
        r"(?:[^\"'\s{}]+|\{[^}]*\})".join(re.escape(p) for p in parts) + r"(?=[\"'?]|$)", re.M
    )
    return bool(pattern.search(source)) and f".{method.lower()}(" in source


def test_every_route_has_a_contract_test() -> None:
    routes = _routes()
    assert len(routes) >= 50, "route discovery must not be vacuous"
    missing = sorted(routes - CONTRACT_TESTS.keys())
    assert missing == [], f"routes without a contract test: {missing}"
    stale = sorted(CONTRACT_TESTS.keys() - routes)
    assert stale == [], f"map entries for routes that no longer exist: {stale}"


def test_mapped_files_actually_call_their_route() -> None:
    wrong = []
    for key, rel in CONTRACT_TESTS.items():
        method, path = key.split(" ", 1)
        source = (TESTS / rel).read_text()
        if not _calls(source, method, path):
            wrong.append(f"{key} -> {rel}")
    assert wrong == [], f"mapped test files that do not call their route: {wrong}"
