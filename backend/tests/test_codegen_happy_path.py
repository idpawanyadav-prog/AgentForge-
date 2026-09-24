import json

from app import codegen


def test_generate_implementation_writes_llm_files(tmp_path, monkeypatch):
    project = {"id": "p1", "name": "P", "goal": "", "technology_stack": "Python",
               "workspace_path": str(tmp_path)}
    task = {"title": "Feature", "description": "", "acceptance_criteria": ""}
    entry = {"ref": "gw:model", "gateway_id": "gw", "model_id": "model",
             "gw": {"id": "gw", "name": "Gateway"},
             "model": {"id": "model", "provider_model_id": "test-model"}}
    monkeypatch.setattr(codegen.toolchains, "detect_stack", lambda *args: "python")
    monkeypatch.setattr(codegen, "_model_chain", lambda *args: ([entry], "Developer"))
    monkeypatch.setattr(codegen.memory, "pitfalls_block", lambda *args: "")
    calls = []
    def fake_llm(*args, **kwargs):
        calls.append(args)
        return {"text": json.dumps({"summary": "Added feature", "files": [
            {"path": "app/feature.py", "content": "VALUE = 42\n"}]}),
            "input_tokens": 10, "output_tokens": 20}
    monkeypatch.setattr(codegen, "call_llm", fake_llm)
    result = codegen.generate_implementation(project, task, "agent")
    assert len(calls) == 1
    assert result["mode"] == "llm"
    assert result["files"] == ["app/feature.py"]
    assert result["pinned_ref"] == "gw:model"
    assert (tmp_path / "app" / "feature.py").read_text() == "VALUE = 42\n"
