"""Global execution event stream routes."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from .. import db
from ..task_registry import background_tasks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["events"])

_sse_connections: set[str] = set()
_HEARTBEAT_INTERVAL = 30
MAX_QUEUE_SIZE = 1000


def _enqueue_event(queue: asyncio.Queue, event: dict) -> None:
    """Keep the newest events when a client consumes more slowly than polling."""
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(event)


async def _sse_generator(queue: asyncio.Queue, conn_id: str, poll_task: asyncio.Task):
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL)
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
            except asyncio.CancelledError:
                break
    finally:
        _sse_connections.discard(conn_id)
        poll_task.cancel()
        logger.debug("SSE connection %s cleaned up", conn_id)


@router.get("/events")
async def global_events(after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
    queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    conn_id = f"sse-{id(queue)}"
    _sse_connections.add(conn_id)
    logger.info("SSE connection %s opened", conn_id)

    async def _poll():
        last_seq = after
        try:
            while conn_id in _sse_connections:
                try:
                    rows = db.query(
                        "SELECT * FROM execution_events WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                        (last_seq, limit),
                    )
                    for row in rows:
                        row["payload"] = json.loads(row["payload"])
                        _enqueue_event(queue, row)
                        last_seq = max(last_seq, row["seq"])
                except Exception as exc:
                    logger.error("SSE poll failed for connection %s: %s", conn_id, exc)
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            raise

    poll_task = asyncio.create_task(_poll(), name=f"{conn_id}-poll")
    background_tasks.track(poll_task)
    return StreamingResponse(
        _sse_generator(queue, conn_id, poll_task),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

