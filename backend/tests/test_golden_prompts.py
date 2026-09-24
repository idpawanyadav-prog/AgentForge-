"""Golden prompt regression tests (AGENT_DEV_SPEEDUP 3.6, offline variant).

The goldens pin the EXACT composed system prompts and template renderings so
an edit to a base system prompt, a role block, or a task template shows up as
a reviewable diff in CI instead of a surprise behavior change in front of a
live sprint. LLM *output* goldens would need a pinned provider; prompt
*composition* is what this codebase controls, so that is what is pinned.

Regenerate after an intentional change:
    UPDATE_GOLDEN=1 python -m pytest tests/test_golden_prompts.py
then review the diff of tests/golden/*.txt.
"""
import os

import pytest

from app import codegen
from app import db as appdb
from app import task_templates as tt

TS = "2026-09-23T00:00:00+00:00"
GOLDEN = os.path.join(os.path.dirname(__file__), "golden")
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


def _check(name: str, actual: str):
    os.makedirs(GOLDEN, exist_ok=True)
    path = os.path.join(GOLDEN, name)
    if UPDATE:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(actual)
        return
    assert os.path.isfile(path), (
        f"golden {name} missing — run with UPDATE_GOLDEN=1 to create it")
    with open(path, encoding="utf-8") as fh:
        expected = fh.read()
    assert actual == expected, (
        f"golden {name} diverged. If the change is intentional, regenerate "
        "with UPDATE_GOLDEN=1 and review the diff.")


def _seed_prompt_source():
    appdb.insert("roles", {"id": "r1", "name": "Senior Developer",
                           "description": "", "active": 1,
                           "created_at": TS, "updated_at": TS})
    appdb.insert("instruction_files", {
        "id": "i1", "role_id": "r1", "filename": "quality.md",
        "description": "", "content": "Prefer explicit types over Any.",
        "version": 1, "active": 1, "created_at": TS, "updated_at": TS})
    persona = {"id": "p1", "role_id": "r1", "name": "Ada",
               "instructions": "Explain tradeoffs in one line per decision.",
               "constraints_text": "Never edit files outside the workspace.",
               "version": 1, "active": 1, "created_at": TS, "updated_at": TS}
    return persona


def test_golden_base_python_system():
    _check("system_base_python.txt", codegen._system_for("python"))


def test_golden_full_composed_system():
    persona = _seed_prompt_source()
    prompt = codegen.build_system_prompt("python", persona, "r1")
    assert "Prefer explicit types over Any." in prompt
    assert "Never edit files outside the workspace." in prompt
    _check("system_composed_python.txt", prompt)


def test_golden_task_template_render():
    applied = tt.apply_template("api-endpoint", {
        "feature": "ticket comments", "method": "POST",
        "resource": "tickets/{id}/comments", "success_status": "201"})
    rendered = "\n\n".join(
        f"{label}:\n{applied[key]}"
        for label, key in (("TITLE", "title"), ("DESCRIPTION", "description"),
                           ("ACCEPTANCE CRITERIA", "acceptance_criteria")))
    _check("template_api_endpoint.txt", rendered)
