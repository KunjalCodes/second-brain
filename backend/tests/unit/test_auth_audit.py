"""Phase 9 structural security audits (docs/PHASE9_PLAN.md sections 8, 13, 15).

Like the Cypher/SQL audits, these inspect STRUCTURE -- the live FastAPI route
table, every request schema reachable from it, every agent tool-argument and
LLM-output schema, and the source tree -- rather than a list of known files,
so a route, schema or module added later is covered automatically.
"""

from __future__ import annotations

import ast
import re
import typing
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.agent.tools import TOOL_ARGS_SCHEMAS
from app.api.deps import get_auth_context, get_current_user_id, require_internal_token
from app.main import app
from app.models.agent import AgentStep
from app.models.extraction import ExtractionResult

APP_DIR = Path(__file__).resolve().parents[2] / "app"
REPO_ROOT = Path(__file__).resolve().parents[3]

#: Routes reachable without a user credential, by design. Everything else under
#: the API must authenticate or be an operator-token internal route.
PUBLIC_ROUTES = {
    ("GET", "/health"),
    ("GET", "/health/ready"),
    ("POST", "/auth/register"),
    ("POST", "/auth/login"),
    # Idempotent: verifies a credential if one is presented, else just clears
    # the cookie (PHASE9_PLAN.md section 9).
    ("POST", "/auth/logout"),
}
#: FastAPI's own schema/docs routes (Q6: kept public; schema, not user data).
DOCS_PATHS = {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}

#: Normalised names that would let a client assert ownership.
OWNERSHIP_NAMES = {"userid", "ownerid", "owner", "accountid", "xuserid"}


def _calls(dependant) -> set:  # noqa: ANN001
    found = {dependant.call}
    for sub in dependant.dependencies:
        found |= _calls(sub)
    return found


def _api_routes() -> list:
    """Every API route with its EFFECTIVE (prefixed) path and dependency tree.

    This FastAPI version nests included routers as `_IncludedRouter` objects
    whose routes are exposed as effective route contexts (path + methods +
    resolved dependant, router-level dependencies included); plain `APIRoute`s
    are taken as-is. Discovery must find every route, or the audits below
    would pass vacuously -- `test_route_discovery_is_not_vacuous` pins that.
    """
    routes: list = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            routes.append(route)
        elif hasattr(route, "effective_route_contexts"):
            routes.extend(route.effective_route_contexts())
    return routes


def _non_api_paths() -> set:
    return {
        r.path
        for r in app.routes
        if not isinstance(r, APIRoute) and not hasattr(r, "effective_route_contexts")
    }


def test_route_discovery_is_not_vacuous() -> None:
    paths = {r.path for r in _api_routes()}
    assert len(_api_routes()) >= 50
    assert {"/memories", "/graph/overview", "/auth/login", "/documents"} <= paths


def _route_ids() -> list[str]:
    return [f"{sorted(r.methods)[0]} {r.path}" for r in _api_routes()]


@pytest.mark.parametrize("route", _api_routes(), ids=_route_ids())
def test_every_route_is_exactly_one_of_public_internal_authenticated(route) -> None:  # noqa: ANN001
    calls = _calls(route.dependant)
    authenticated = bool({get_current_user_id, get_auth_context} & calls)
    internal = require_internal_token in calls
    public = any((m, route.path) in PUBLIC_ROUTES for m in route.methods)
    assert [authenticated, internal, public].count(True) == 1, (
        f"{route.methods} {route.path}: authenticated={authenticated} "
        f"internal={internal} public={public}"
    )


def test_non_api_routes_are_only_the_docs() -> None:
    others = _non_api_paths()
    assert others <= DOCS_PATHS, others - DOCS_PATHS


def test_the_public_allow_list_matches_real_routes() -> None:
    """A stale allow-list entry would silently cover a future route of that name."""
    real = {(m, r.path) for r in _api_routes() for m in r.methods}
    assert real >= PUBLIC_ROUTES


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def _model_field_names(model: type[BaseModel], seen: set | None = None) -> set[str]:
    """Every field name in a model, recursing into nested models and generics."""
    seen = seen if seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)
    names: set[str] = set()
    for field_name, info in model.model_fields.items():
        names.add(field_name)
        if info.alias:
            names.add(info.alias)
        for arg in _nested_models(info.annotation):
            names |= _model_field_names(arg, seen)
    return names


def _nested_models(annotation: object) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    out: list[type[BaseModel]] = []
    for arg in typing.get_args(annotation):
        out.extend(_nested_models(arg))
    return out


def _request_param_names(dependant) -> set[str]:  # noqa: ANN001
    names = set()
    for group in (
        dependant.path_params,
        dependant.query_params,
        dependant.header_params,
        dependant.cookie_params,
        dependant.body_params,
    ):
        for param in group:
            names.add(param.name)
            names.add(getattr(param, "alias", param.name))
            for model in _nested_models(param.field_info.annotation):
                names |= _model_field_names(model)
    for sub in dependant.dependencies:
        names |= _request_param_names(sub)
    return names


@pytest.mark.parametrize("route", _api_routes(), ids=_route_ids())
def test_no_request_input_can_carry_an_ownership_id(route) -> None:  # noqa: ANN001
    offenders = {
        n for n in _request_param_names(route.dependant) if _normalise(n) in OWNERSHIP_NAMES
    }
    assert offenders == set(), f"{route.path} accepts client-supplied ownership: {offenders}"


@pytest.mark.parametrize(
    "model",
    [*TOOL_ARGS_SCHEMAS.values(), AgentStep, ExtractionResult],
    ids=lambda m: m.__name__,
)
def test_no_llm_facing_schema_can_express_an_ownership_id(model: type[BaseModel]) -> None:
    """Agent tool arguments and LLM structured outputs (agent steps,
    extraction proposals) must have no field an LLM could use to pick a user."""
    offenders = {n for n in _model_field_names(model) if _normalise(n) in OWNERSHIP_NAMES}
    assert offenders == set()


@pytest.mark.parametrize("model", [*TOOL_ARGS_SCHEMAS.values()], ids=lambda m: m.__name__)
def test_tool_argument_schemas_forbid_extra_fields(model: type[BaseModel]) -> None:
    assert model.model_config.get("extra") == "forbid"


def test_only_the_identity_dependency_reads_the_dev_header() -> None:
    """No module other than app/api/deps.py may read `X-User-Id` -- a second
    reader would be a second, unreviewed identity source."""
    readers = []
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.lower() == "x-user-id"
                and path.name not in {"deps.py", "main.py"}
            ):
                readers.append(str(path.relative_to(APP_DIR)))
    assert readers == []


def test_routes_never_take_identity_from_the_request_object() -> None:
    """Route handlers get `user_id` only via `CurrentUser`; none reads a user
    id from headers/query/body themselves."""
    offenders = []
    for path in (APP_DIR / "api" / "routes").glob("*.py"):
        source = path.read_text()
        if re.search(r"request\.(headers|query_params|cookies)\.get\([\"']x?-?user", source, re.I):
            offenders.append(path.name)
    assert offenders == []


def test_env_example_contains_no_secret_values() -> None:
    secret_key = re.compile(r"(SECRET|PASSWORD|TOKEN|API_KEY|_KEY)$")
    offenders = []
    for line in (REPO_ROOT / ".env.example").read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        if secret_key.search(key.strip()) and value.strip():
            offenders.append(key.strip())
    assert offenders == [], f".env.example must not contain secret values: {offenders}"


def test_rate_limited_route_classes_are_wired() -> None:
    """Every route in each abuse-sensitive class carries its limiter."""
    expected = {
        ("POST", "/search"): "search",
        ("GET", "/memories/search"): "search",
        ("GET", "/entities/search"): "search",
        ("GET", "/entities/{entity_id}/graph"): "graph",
        ("GET", "/graph/overview"): "graph",
        ("GET", "/graph/entities/{entity_id}/neighborhood"): "graph",
        ("GET", "/graph/edges/evidence"): "graph",
        ("GET", "/graph/memories/{memory_id}/timeline"): "graph",
        ("POST", "/conversations/{conversation_id}/agent-turn"): "agent",
        ("POST", "/conversations/{conversation_id}/messages"): "message",
        ("POST", "/conversations"): "write",
        ("POST", "/documents"): "ingest",
        ("POST", "/memories"): "write",
        ("PATCH", "/memories/{memory_id}"): "write",
        ("POST", "/memories/{memory_id}/invalidate"): "write",
    }
    for route in _api_routes():
        if route.path.startswith("/review/") and "POST" in route.methods:
            expected[("POST", route.path)] = "write"
    wired = {}
    for route in _api_routes():
        names = {getattr(c, "__name__", "") for c in _calls(route.dependant)}
        classes = {n.removeprefix("rate_limited_") for n in names if n.startswith("rate_limited_")}
        for method in route.methods:
            if classes:
                wired[(method, route.path)] = classes
    for key, limit_class in expected.items():
        assert wired.get(key) == {limit_class}, (key, wired.get(key))
