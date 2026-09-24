"""Dev-loop tools from AGENT_DEV_SPEEDUP: DRY_RUN stubs, DEBUG_PROMPTS
tracing, and PERSONA_OVERRIDE local prompt files."""
import asyncio
import json
import os

from app import codegen


def test_dry_run_call_llm_returns_stub_without_gateway(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    res = codegen.call_llm({"id": "gw-missing"}, {"provider_model_id": "m"},
                           "system", "user")
    assert res["dry_run"] is True
    assert res["input_tokens"] == 0 and res["output_tokens"] == 0
    obj = json.loads(res["text"])
    assert obj["files"] == []


def test_dry_run_async_path(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    res = asyncio.run(codegen.acall_llm({"id": "gw"}, {"provider_model_id": "m"},
                                        "s", "u"))
    assert res["dry_run"] is True


def test_dry_run_off_still_requires_key(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    try:
        codegen.call_llm({"id": "gw-missing"}, {"provider_model_id": "m"}, "s", "u")
        assert False, "expected RuntimeError for missing key"
    except RuntimeError as exc:
        assert "No API key" in str(exc)


def test_debug_prompts_trace_written(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("DEBUG_PROMPTS", "1")
    monkeypatch.setenv("DEBUG_PROMPTS_DIR", str(tmp_path))
    codegen.call_llm({"id": "g"}, {"provider_model_id": "m1"},
                     "SYSTEM TEXT", "USER TEXT", trace_label="test-label")
    files = list((tmp_path / "test-label").iterdir())
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "===== SYSTEM PROMPT =====" in body
    assert "SYSTEM TEXT" in body and "USER TEXT" in body
    assert "m1" in files[0].name


def test_debug_prompts_off_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.delenv("DEBUG_PROMPTS", raising=False)
    monkeypatch.setenv("DEBUG_PROMPTS_DIR", str(tmp_path))
    codegen.call_llm({"id": "g"}, {"provider_model_id": "m"}, "s", "u",
                     trace_label="quiet")
    assert not (tmp_path / "quiet").exists()


def _write_override(tmp_path, **kw):
    path = tmp_path / "persona.json"
    path.write_text(json.dumps(kw), encoding="utf-8")
    return str(path)


def test_persona_override_replaces_db_persona(monkeypatch, tmp_path):
    ov = _write_override(
        tmp_path,
        role_instructions=[{"name": "Reviewer Rules", "content": "ALWAYS check types"}],
        persona={"name": "Lab Persona", "instructions": "Be terse",
                 "constraints_text": "No globals"})
    monkeypatch.setenv("PERSONA_OVERRIDE", ov)
    prompt = codegen.build_system_prompt(
        "python", {"name": "DB Persona", "instructions": "DB instructions"}, "role-1")
    assert "Be terse" in prompt and "No globals" in prompt
    assert "ALWAYS check types" in prompt and "Reviewer Rules" in prompt
    assert "DB instructions" not in prompt
    assert "Lab Persona" in prompt


def test_persona_override_junior_switches_base(monkeypatch, tmp_path):
    full = codegen.build_system_prompt("python", None, None)
    monkeypatch.setenv("PERSONA_OVERRIDE", _write_override(tmp_path, junior=True))
    junior = codegen.build_system_prompt("python", None, None)
    assert junior != full
    assert codegen._JR_DEV_SYSTEM.strip()[:40] in junior


def test_persona_override_invalid_file_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("PERSONA_OVERRIDE", str(tmp_path / "nope.json"))
    assert codegen._persona_override() is None
