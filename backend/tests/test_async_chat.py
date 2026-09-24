import asyncio

from app import chatbot, db
from app import codegen


def test_repaired_partial_command_is_not_executed():
    assert chatbot._extract_json('{"reply":"OK","command":{"name":"add_task","args":{"title":"Par') is None
    assert chatbot._extract_json('{"reply":"OK","command":null}') == {
        "reply": "OK", "command": None}


def test_async_chat_uses_async_gateway(monkeypatch):
    ts = db.now()
    db.insert("projects", {"id": "p-chat", "name": "Chat", "goal": "",
                           "description": "", "workspace_path": "", "created_at": ts,
                           "updated_at": ts})
    db.insert("conversations", {"id": "c-chat", "project_id": "p-chat",
                                "title": "New conversation", "created_at": ts,
                                "updated_at": ts})
    monkeypatch.setattr(chatbot, "_bot_config", lambda: ({"id": "gw"}, {"id": "model"}))
    monkeypatch.setattr(chatbot, "_context_brief", lambda _pid: "context")
    async def fake_call(cls, gw, model, message, system_prompt="", max_tokens=8000, **kw):
        return {"text": '{"reply":"Hello","command":null,"suggestions":["Status"]}'}
    monkeypatch.setattr(codegen.GatewayClient, "acall", classmethod(fake_call))
    result = asyncio.run(chatbot.handle_message_async("p-chat", "c-chat", "Hello there"))
    assert result["reply"] == "Hello"
    assert [m["role"] for m in db.query("SELECT role FROM messages ORDER BY created_at, rowid")] == [
        "user", "assistant"]


def test_async_gateway_retry_uses_async_backoff(monkeypatch):
    attempts = []
    sleeps = []
    async def fake_llm(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Could not reach gateway: temporary failure")
        return {"text": "ok", "input_tokens": 1, "output_tokens": 1}
    async def fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(codegen, "acall_llm", fake_llm)
    monkeypatch.setattr(codegen.asyncio, "sleep", fake_sleep)
    result = asyncio.run(codegen.GatewayClient.acall(
        {"id": "gw-async-retry", "name": "Gateway"}, {"id": "model"}, "hello"))
    assert result["text"] == "ok"
    assert len(attempts) == 2
    assert sleeps == [1]
