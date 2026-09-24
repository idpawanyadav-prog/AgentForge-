"""Regression checks for the issue-plan correctness and resource bounds."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import pytest

from app import chatbot, codegen, db, governance, runtime
from app.routers import events, tasks


def test_llm_outage_and_generated_file_limits():
    assert not codegen.is_llm_outage("")
    assert codegen.is_llm_outage("Gateway returned HTTP 503")
    assert codegen.is_llm_outage("timeout exceeded")
    assert not codegen._validate_generated_files([("a.py", "x" * 100)])
    assert "exceeds size limit" in codegen._validate_generated_files(
        [("a.py", "x" * (1_048_576 + 1))])
    assert "Total generated size" in codegen._validate_generated_files(
        [(f"{i}.py", "x" * 1_000_000) for i in range(6)])


def test_gateway_health_is_isolated():
    runtime._gateway_health.clear()
    for _ in range(runtime._LLM_HEALTH_THRESHOLD):
        runtime.mark_llm_failed("gateway-a")
    assert not runtime.is_llm_healthy("gateway-a")
    assert runtime.is_llm_healthy("gateway-b")
    runtime.mark_llm_healthy("gateway-a")
    assert runtime.is_llm_healthy("gateway-a")
    assert runtime._gateway_health["gateway-a"]["consecutive_failures"] == 0


def test_pending_baseline_uniqueness_under_concurrent_proposals():
    ts = db.now()
    db.insert("projects", {"id": "p1", "name": "P", "goal": "",
                           "description": "", "workspace_path": "C:/none",
                           "status": "Active", "lifecycle_state": "Draft",
                           "created_at": ts, "updated_at": ts})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: governance.propose_baseline("p1"), range(2)))
    assert sum("baseline" in result for result in results) == 1
    assert sum("error" in result for result in results) == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM project_baselines")["n"] == 1


def test_summary_cache_is_bounded(monkeypatch):
    tasks._summary_cache.clear()
    monkeypatch.setattr(tasks, "_build_summary", lambda pid: {"project": pid})
    for i in range(110):
        tasks.control_summary(str(i))
    assert len(tasks._summary_cache) == tasks._MAX_SUMMARY_CACHE_SIZE
    assert "0" not in tasks._summary_cache
    assert tasks.control_summary("109") == {"project": "109"}


def test_sse_queue_keeps_latest_events():
    queue = asyncio.Queue(maxsize=2)
    for seq in range(3):
        events._enqueue_event(queue, {"seq": seq})
    assert queue.qsize() == 2
    assert [queue.get_nowait()["seq"] for _ in range(2)] == [1, 2]


def test_repaired_chatbot_json_requires_complete_command():
    assert chatbot._extract_json('{"reply":"Done","command":null}') == {
        "reply": "Done", "command": None}
    assert chatbot._extract_json('{"reply":"Do it","command":{"name":"delete"') is None
    assert chatbot._extract_json('{"reply":"Do it","command":{"name":"delete","args":{}}}')
    assert chatbot._extract_json('{"reply":"Do it","command":{"name":"delete","args":[]}}') is None


def test_every_typed_mutation_has_a_handler():
    informational = {"cancel_pending", "confirm", "help", "show_pending", "status"}
    typed_commands = {name for name, _ in chatbot.INTENTS} - informational
    assert typed_commands == set(chatbot._COMMAND_HANDLERS)


def test_pending_command_executes_once_under_concurrent_confirms(monkeypatch):
    ts = db.now()
    db.insert("projects", {"id": "p1", "name": "P", "workspace_path": "C:/none",
                           "created_at": ts, "updated_at": ts})
    db.insert("conversations", {"id": "c1", "project_id": "p1", "title": "T",
                                "created_at": ts, "updated_at": ts})
    chatbot._queue_pending("c1", "test", {}, "test")
    calls = []
    monkeypatch.setattr(chatbot, "_execute_command",
                        lambda project, command, args: calls.append(command) or "done")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: chatbot._confirm_pending("p1", "c1"), range(2)))
    assert calls == ["test"]
    assert sorted(result["reply"] for result in results) == [
        "There is no pending command to confirm.", "done"]


def test_execution_route_passes_idempotency_header(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    seen = []

    def fake_start(project_id, task_id, idempotency_key=None):
        seen.append((project_id, task_id, idempotency_key))
        return {"run": {"id": "run-1"}}

    monkeypatch.setattr(runtime, "start_execution", fake_start)
    response = TestClient(app).post(
        "/api/v1/executions", json={"project_id": "p1", "task_id": "t1"},
        headers={"Idempotency-Key": "same-request"})
    assert response.status_code == 200
    assert seen == [("p1", "t1", "same-request")]


def test_generate_implementation_rejects_oversized_output_before_write(tmp_path, monkeypatch):
    project = {"id": "p1", "name": "P", "workspace_path": str(tmp_path),
               "goal": "", "technology_stack": "Python"}
    task = {"title": "Feature", "description": "", "acceptance_criteria": ""}
    monkeypatch.setattr(codegen.toolchains, "detect_stack", lambda *args: "python")
    monkeypatch.setattr(codegen, "_model_chain", lambda *args: ([], ""))
    monkeypatch.setattr(codegen, "_scaffold_files",
                        lambda *args: [("app/too_big.py", "x" * (1_048_576 + 1))])
    result = codegen.generate_implementation(project, task, "agent")
    assert "exceeds size limit" in result["error"]
    assert not (tmp_path / "app" / "too_big.py").exists()


def test_lifespan_initializes_database_and_runtime(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    calls = []
    monkeypatch.setattr(main.db, "init_db", lambda: calls.append("db"))
    monkeypatch.setattr(main.workspace, "relocate_workspaces", lambda: calls.append("workspace"))
    monkeypatch.setattr(main.workspace, "recover_merge_journals", lambda: calls.append("merge_recovery"))
    monkeypatch.setattr(main.runtime, "recover_orphans", lambda: calls.append("recovery"))
    monkeypatch.setattr(main.runtime, "set_loop", lambda _: calls.append("loop"))
    with TestClient(main.app) as client:
        assert client.get("/api/v1/health").status_code == 200
    assert calls == ["db", "workspace", "merge_recovery", "recovery", "loop"]


def test_gateway_discovery_fetch_is_async(monkeypatch):
    import httpx
    from app.routers import gateways

    monkeypatch.setattr(gateways.db, "get_gateway_key", lambda _: "secret")
    monkeypatch.setattr(gateways.socket, "getaddrinfo", lambda *_: [
        (None, None, None, None, ("8.8.8.8", 443))])
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"data": [{"id": "model-a"}, {"id": "model-b"}]}))
    original_client = httpx.AsyncClient
    monkeypatch.setattr(gateways.httpx, "AsyncClient",
                        lambda **kwargs: original_client(transport=transport, **kwargs))
    result = asyncio.run(gateways._live_model_ids(
        {"id": "gw", "base_url": "https://example.test/v1"}))
    assert result == {"ok": True, "ids": ["model-a", "model-b"], "error": None}


def test_gateway_discovery_rejects_metadata_and_normalizes_version(monkeypatch):
    from app.routers import gateways
    monkeypatch.setattr(gateways.socket, "getaddrinfo", lambda *_: [
        (None, None, None, None, ("169.254.169.254", 80))])
    with pytest.raises(ValueError, match="private or reserved"):
        asyncio.run(gateways._models_discovery_url("http://169.254.169.254"))
    monkeypatch.setattr(gateways.socket, "getaddrinfo", lambda *_: [
        (None, None, None, None, ("127.0.0.1", 8000))])
    assert asyncio.run(gateways._models_discovery_url("http://localhost:8000/Api/V1/")) == \
        "http://localhost:8000/Api/V1/models"


def test_rate_limit_uses_shared_backend_when_configured(monkeypatch):
    from types import SimpleNamespace
    from app import rate_limit

    calls = []

    class FakeRedis:
        def eval(self, script, key_count, key, now, window, limit, member):
            calls.append((key, window, limit, member))
            return 0

    monkeypatch.setattr(rate_limit, "_redis", FakeRedis())
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), method="POST")
    assert not rate_limit._allow(request)
    assert calls[0][:3] == ("agentforge:rate:127.0.0.1:POST",
                            rate_limit.WINDOW_S, rate_limit.MUTATION_LIMIT)


def test_circuit_breaker_cooldown_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(codegen, "_BREAKER_STATE_FILE", str(tmp_path / "breakers.json"))
    breaker = codegen._BreakerState()
    for _ in range(5):
        breaker.record_failure("provider unavailable")
    codegen._save_breakers({"gateway-a": breaker})
    restored = codegen._load_breakers()
    assert restored["gateway-a"].is_open
    assert not restored["gateway-a"].is_half_open
    assert restored["gateway-a"].last_failure == "prior provider failure"
    restored["gateway-a"].record_success()
    codegen._save_breakers(restored)
    assert not codegen._load_breakers()
