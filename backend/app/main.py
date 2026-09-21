"""AgentForge API — FastAPI application factory.

Mounts all routers, configures middleware, and serves the SPA.
All route handlers live in the ``routers/`` package; this file owns only
app creation, lifespan, middleware, router inclusion, and static assets.
"""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import chatbot, db, po, runtime, workspace
from .routers import agents, chat, gateways, models, projects, roles, settings, tasks
from .rate_limit import rate_limit_middleware

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "static")

# Track active SSE connections for clean disconnect handling.
_sse_connections: set = set()
_HEARTBEAT_INTERVAL = 30


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    workspace.relocate_workspaces()
    runtime.recover_orphans()
    runtime.set_loop(asyncio.get_running_loop())

    async def _resume_po_projects():
        await asyncio.sleep(2.0)
        try:
            projects = db.query(
                "SELECT p.id FROM projects p WHERE p.po_enabled = 1 AND EXISTS ("
                "  SELECT 1 FROM tasks t WHERE t.project_id = p.id AND t.sprint_id IS NOT NULL "
                "  AND t.status NOT IN ('Done','Cancelled'))")
            for p in projects:
                if p["id"] in runtime._schedulers:
                    logger.warning(
                        "PO resume: scheduler already running for project %s; skipping",
                        p["id"])
                    continue
                result = runtime.start_sprint_execution(p["id"])
                if "error" in result:
                    logger.warning(
                        "PO resume failed for project %s: %s",
                        p["id"], result["error"])
                else:
                    logger.info(
                        "PO resume started sprint execution for project %s",
                        p["id"])
        except Exception as exc:
            logger.error("PO resume failed: %s", exc, exc_info=True)

    asyncio.create_task(_resume_po_projects())
    yield


app = FastAPI(title="AgentForge API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting (per client IP + method, sliding window).
app.middleware("http")(rate_limit_middleware)

# Mount routers
app.include_router(gateways.router)
app.include_router(roles.router)
app.include_router(models.router)
app.include_router(agents.router)
app.include_router(projects.router)
app.include_router(tasks.router)
app.include_router(chat.router)
app.include_router(settings.router)


async def _sse_generator(queue: asyncio.Queue, conn_id: str):
    """Yield SSE-formatted events with periodic heartbeats."""
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL)
                payload = json.dumps(event)
                yield f"data: {payload}\n\n"
            except asyncio.TimeoutError:
                yield f": heartbeat\n\n"
            except asyncio.CancelledError:
                break
    finally:
        _sse_connections.discard(conn_id)
        logger.debug("SSE connection %s cleaned up", conn_id)


@app.get("/api/v1/events")
async def global_events(after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
    """SSE endpoint streaming all project events."""
    from ..db import query as _query
    queue: asyncio.Queue = asyncio.Queue()
    conn_id = f"sse-{id(queue)}"
    _sse_connections.add(conn_id)
    logger.info("SSE connection %s opened", conn_id)

    async def _poll():
        last_seq = after
        while conn_id in _sse_connections:
            try:
                rows = _query(
                    "SELECT * FROM execution_events WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                    (last_seq, limit))
                for row in rows:
                    row["payload"] = json.loads(row["payload"])
                    await queue.put(row)
                    last_seq = max(last_seq, row["seq"])
            except Exception as exc:
                logger.error("SSE poll failed for connection %s: %s", conn_id, exc)
            await asyncio.sleep(2.0)

    asyncio.get_running_loop().create_task(_poll())
    return StreamingResponse(_sse_generator(queue, conn_id),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# Static frontend
if os.path.isdir(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        candidate = os.path.join(STATIC_DIR, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
