"""Extraction observability and the internal sweep trigger.

Two routers, deliberately separate:

* `router` (`/extraction/...`) -- user-scoped job/candidate status, behind the same
  `X-User-Id` identity as everything else.
* `internal_router` (`/internal/extraction/...`) -- NOT user-scoped, guarded by
  `X-Internal-Token` instead (`InternalTokenDep`). This is what a managed host's own
  cron calls to run the sweep (docs/adr/0008); it is not part of the public API and
  must never be exposed to end users.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import (
    CurrentUser,
    EmbeddingClientDep,
    ExtractionStatusServiceDep,
    InternalTokenDep,
    LLMClientDep,
    SettingsDep,
)
from app.db.engine import get_database
from app.extraction.runner import run_sweep_tick
from app.graph.connection import get_graph_client
from app.schemas.extraction import ExtractionJobDetail, ExtractionJobListResponse, SweepResult

router = APIRouter(prefix="/extraction", tags=["extraction"])
internal_router = APIRouter(prefix="/internal/extraction", tags=["internal"])


@router.get("/jobs", response_model=ExtractionJobListResponse, summary="List extraction jobs")
async def list_extraction_jobs(
    user_id: CurrentUser,
    service: ExtractionStatusServiceDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExtractionJobListResponse:
    return await service.list_jobs(
        user_id=user_id, limit=min(limit, settings.search_max_limit), offset=offset
    )


@router.get(
    "/jobs/{job_id}",
    response_model=ExtractionJobDetail,
    summary="Get one extraction job and its candidates",
)
async def get_extraction_job(
    job_id: str,
    user_id: CurrentUser,
    service: ExtractionStatusServiceDep,
) -> ExtractionJobDetail:
    """Every candidate the job produced, whether or not it became a memory --
    this is the audit trail between "LLM proposed" and "engine decided"
    (docs/PHASE2_PLAN.md section 4).
    """
    return await service.get_job(user_id=user_id, job_id=job_id)


@internal_router.post(
    "/sweep",
    response_model=SweepResult,
    summary="Run one bounded sweep tick (internal; requires X-Internal-Token)",
)
async def trigger_sweep(
    _: InternalTokenDep,
    llm_client: LLMClientDep,
    embedding_client: EmbeddingClientDep,
    settings: SettingsDep,
) -> SweepResult:
    """Reclaim expired leases, re-evaluate the gate for skipped messages, and
    process a batch of claimable jobs -- bounded and idempotent (docs/adr/0008).
    Intended to be called by the managed host's own cron; see scripts/run_sweep.py
    for the local equivalent.
    """
    counts = await run_sweep_tick(
        db=get_database(),
        graph_client=get_graph_client(),
        llm_client=llm_client,
        embedding_client=embedding_client,
        settings=settings,
    )
    return SweepResult(**counts)
