import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from app import results_store
from app.auth import verify_request
from app.db import db_session, get_connection
from app.deps import get_scoring_port, get_session_manager, get_sourcing_port
from app.ports import ScoringPort, SourcingPort
from app.run_lock import run_lock  # the ONE module-level singleton
from app.run_orchestrator import RunEvent, RunOrchestrator
from app.session_port import SessionManager

router = APIRouter(dependencies=[Depends(verify_request)])


def format_sse(event: RunEvent) -> str:
    """Byte-exact wire format consumed by Slice 6's hand-rolled fetch() parser:
    'event: <name>\\ndata: <single-line json>\\n\\n'."""
    return f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"


async def _stream(
    sourcing: SourcingPort, scoring: ScoringPort, session: SessionManager
) -> AsyncIterator[str]:
    conn = get_connection()
    try:
        orchestrator = RunOrchestrator(sourcing, scoring, session, conn)
        # aclosing(): on client disconnect, finalize the orchestrator (marks the
        # run 'error') BEFORE its connection is closed below.
        async with aclosing(orchestrator.execute_run()) as events:
            async for event in events:
                yield format_sse(event)
    finally:
        conn.close()
        run_lock.release()


@router.post("/run")
async def start_run(
    sourcing: Annotated[SourcingPort, Depends(get_sourcing_port)],
    scoring: Annotated[ScoringPort, Depends(get_scoring_port)],
    session: Annotated[SessionManager, Depends(get_session_manager)],
):
    """409 JSON (not a stream) if a run is already in flight (AC17); otherwise
    a text/event-stream of RunEvents. The lock is released in the stream
    generator's finally, i.e. once the run finishes, errors, or the client
    disconnects."""
    if not run_lock.try_acquire():
        return JSONResponse(
            status_code=409, content={"error": "run already in progress"}
        )
    return StreamingResponse(
        _stream(sourcing, scoring, session),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/run/status")
async def run_status():
    return {"in_progress": run_lock.in_progress}


@router.get("/run/{run_id}/results")
async def run_results(
    run_id: int,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=results_store.MAX_PAGE_SIZE)
    ] = results_store.DEFAULT_PAGE_SIZE,
):
    """One page of a run's shown results, best score first. run_complete
    carries page 1 with the default page_size; later pages come from here.
    404 for an unknown run_id; a page past the end has empty `results`."""
    with db_session() as conn:
        if not results_store.run_exists(conn, run_id):
            raise HTTPException(status_code=404, detail="run not found")
        return results_store.get_results_page(conn, run_id, page, page_size)


@router.get("/run/last")
async def run_last():
    """Most recent run_history row (any status), for hydrating the topbar
    summary + results grid on a fresh page load/reload -- no other route
    exposes this. {"has_run": false} if run_history is empty."""
    with db_session() as conn:
        row = conn.execute(
            "SELECT id, started_at, finished_at, status, chat_direction, "
            "new_count, seen_hidden_count, failed_sources "
            "FROM run_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return {"has_run": False}
    return {
        "has_run": True,
        "run_id": row["id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "chat_direction": row["chat_direction"],
        "new_count": row["new_count"],
        "seen_hidden_count": row["seen_hidden_count"],
        "failed_sources": json.loads(row["failed_sources"] or "[]"),
    }
