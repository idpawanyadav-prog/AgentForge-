"""Chat, PO-chat, and conversation routes."""
from __future__ import annotations

import asyncio as _asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import chatbot, po
from ..db import audit, execute, insert, new_id, now, query, query_one, update
from ._util import or_404 as _or_404

router = APIRouter(prefix="/api/v1", tags=["chat"])


class MessageIn(BaseModel):
    content: str


class PoChatIn(BaseModel):
    content: str
    attachments: list = []


# ------------------ product owner ------------------

@router.post("/projects/{pid}/po/enable")
def enable_po_agent(pid: str):
    _or_404(query_one("SELECT id FROM projects WHERE id=?", (pid,)), "Project")
    result = po.set_po_enabled(pid, True)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/projects/{pid}/po/disable")
def disable_po_agent(pid: str):
    _or_404(query_one("SELECT id FROM projects WHERE id=?", (pid,)), "Project")
    result = po.set_po_enabled(pid, False)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.get("/projects/{pid}/po/messages")
def po_messages(pid: str):
    _or_404(query_one("SELECT id FROM projects WHERE id=?", (pid,)), "Project")
    conv = query_one("SELECT id FROM conversations WHERE project_id=? AND title='Product Owner' "
                     "ORDER BY created_at LIMIT 1", (pid,))
    if not conv:
        return []
    return query("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, rowid", (conv["id"],))


@router.post("/projects/{pid}/po/chat")
async def po_chat(pid: str, body: PoChatIn):
    _or_404(query_one("SELECT id FROM projects WHERE id=?", (pid,)), "Project")
    if not body.content.strip() and not body.attachments:
        raise HTTPException(400, "Message content is required")
    result = await _asyncio.get_running_loop().run_in_executor(
        None, po.handle_po_message, pid, body.content.strip(), body.attachments)
    return result


# ------------------ conversations ------------------

@router.get("/projects/{pid}/conversations")
def list_conversations(pid: str):
    return query("SELECT * FROM conversations WHERE project_id=? ORDER BY updated_at DESC", (pid,))


@router.post("/projects/{pid}/conversations")
def create_conversation(pid: str):
    cid = new_id()
    ts = now()
    insert("conversations", {"id": cid, "project_id": pid, "title": "New conversation",
                             "created_at": ts, "updated_at": ts})
    return query_one("SELECT * FROM conversations WHERE id = ?", (cid,))


@router.get("/conversations/{cid}/messages")
def get_messages(cid: str):
    _or_404(query_one("SELECT id FROM conversations WHERE id=?", (cid,)), "Conversation")
    return query("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, rowid", (cid,))


@router.delete("/conversations/{cid}")
def delete_conversation(cid: str):
    execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
    execute("DELETE FROM pending_commands WHERE conversation_id=?", (cid,))
    execute("DELETE FROM conversations WHERE id=?", (cid,))
    return {"ok": True}


@router.post("/projects/{pid}/conversations/{cid}/messages")
async def post_message(pid: str, cid: str, body: MessageIn):
    _or_404(query_one("SELECT id FROM conversations WHERE id=? AND project_id=?", (cid, pid)),
            "Conversation")
    if not body.content.strip():
        raise HTTPException(400, "Message content is required")
    result = await chatbot.handle_message_async(pid, cid, body.content)
    messages = query("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, rowid", (cid,))
    return {**result, "messages": messages}
