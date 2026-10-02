# Phase 17 — Evaluation map (M18)

Separates *system mechanics work* (deterministic) from *real model behaviour works* (needs a
real model). Nothing here reports a result that was not produced by a run.

| Layer | What it proves | Where | Needs | Automated where |
|---|---|---|---|---|
| Unit | pure logic: governance, temporal rules, ids, parsers, schemas, route inventory | `backend/tests/unit/` (61 files) | nothing | every push (`ci.yml`) |
| Integration | real HTTP + real PostgreSQL + real Neo4j: auth, isolation, lifecycle, retrieval, graph API, projects, events, Git, resume, concurrency | `backend/tests/integration/` (50 files) | Postgres + Neo4j | opt-in/weekly `integration.yml` (secrets) |
| Frontend unit | components, API client, wording, a11y labels | `frontend/tests/unit/` (Vitest) | nothing | every push (`ci.yml`) |
| Frontend E2E | real browser against a real backend: auth, memory, graph, review, documents, projects/space switching, a11y (axe), CSP, isolation | `frontend/e2e/` (Playwright) | built frontend + backend | manual `e2e.yml` |
| Adapter tests | MCP tool surface, request routing, path-traversal guard, error mapping | `integrations/mcp/tests/` | adapter venv | opt-in/weekly `integration.yml` |
| Scripted deterministic evals | retrieval quality, document extraction, extraction runner, memory quality, agent tools, graph intelligence, project memory | `backend/eval/*_runner.py` | fake LLM/embeddings + databases | opt-in/weekly `integration.yml` (all runners except `agent_runner.py`) |
| System scenario | one 23-step journey over the **live** stack through a real stdio MCP connection and the real Git collector | `backend/eval/system_scenario.py` | running engine; adapter venv | no (manual) |
| FakeLLM agent tests | the internal Memory Agent's loop and tool dispatch with scripted model output | `tests/integration/test_agent_api.py`, `eval/agent_runner.py` | nothing | in the integration suite |
| Real-LLM extraction | whether a real model extracts good candidates | opt-in benchmarks (`eval/opt_in.py`) | provider key | manual `real-llm-eval.yml` (agent + document) |
| Real external agent | whether a real coding agent *chooses to use* the tools and benefits from continuity | `eval/real_agent_continuity.py` | Claude access | **no — run once in Phase 16; not repeated** |

## What each layer can and cannot say

* **Mechanics (deterministic layers).** An event sent through the REST/MCP surface is stored, projected,
  isolated, queryable and resumable; Git history is idempotent; credentials are scoped. These are
  *system* claims and are tested.
* **Model behaviour (real-LLM layers).** Whether a model extracts the right facts, calls the right tool,
  or produces better answers with memory is an *empirical* claim. The only real-agent run is
  reported in `PHASE16_REPORT.md` with its limits; Phase 17 did **not** re-run it. **Real-agent rerun
  recommended** after the Phase 17 fixes (agent write path, MCP guard), because the first run
  predates them. It is not run automatically (it consumes Claude usage).
* The system scenario's "agent" is a script playing the part of a coding agent. It proves the MCP
  plumbing and the engine, not that any model will call these tools.

## CI gaps found (documented, not changed)

* `ci.yml` lints `app tests ../scripts` only: `backend/eval/` and `integrations/mcp/` are not
  linted. The database-backed suites, deterministic evals and adapter tests run only in the
  opt-in/weekly workflow, so a push alone does not exercise them. [L]
* `scripts/check_consistency.py` does not yet cover Phase 16 tables/spaces. [L]
