"""AgentForge API — FastAPI application factory.

Mounts all routers, configures middleware, and serves the SPA.
All route handlers live in the ``routers/`` package; this file owns only
app creation, lifespan, middleware, router inclusion, and static assets.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import chatbot, db, po, runtime, workspace
from .routers import agents, chat, events, gateways, models, projects, roles, settings, tasks
from .rate_limit import rate_limit_middleware
from .task_registry import background_tasks

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))


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

    background_tasks.create(_resume_po_projects(), name="po-resume")
    try:
        yield
    finally:
        n = background_tasks.cancel_all()
        if n:
            logger.info("Cancelled %d background task(s) on shutdown", n)


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
app.include_router(events.router)


# Static frontend
if os.path.isdir(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        candidate = os.path.join(STATIC_DIR, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
