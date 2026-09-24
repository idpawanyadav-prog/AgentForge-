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


@pytest.mark.parametrize("stack", ["dotnet", "go", "node"])
def test_golden_base_system_per_stack(stack):
    _check(f"system_base_{stack}.txt", codegen._system_for(stack))


def test_golden_junior_dev_system():
    # The Junior Developer has its own codegen system prompt (reusable-helper
    # specialty), so its exact text is pinned like the stack bases.
    _check("system_jr_dev.txt", codegen._JR_DEV_SYSTEM)


@pytest.mark.parametrize("mode", ["sa", "ba"])
def test_golden_review_gate_systems(mode):
    _check(f"system_review_{mode}.txt", codegen._REVIEW_SYSTEM[mode])


def test_golden_task_plan_system():
    # The Product Owner's sprint-planning prompt is the last in-code role
    # prompt that shapes agent behavior and deserves a pin.
    from app import runtime
    _check("system_task_plan.txt", runtime._TASK_PLAN_SYSTEM)


def test_golden_approval_system():
    _check("system_approval.txt", codegen._APPROVE_SYSTEM)


def test_golden_role_kit_prompt():
    from app import chatbot
    _check("prompt_role_kit.txt", chatbot._ROLE_KIT_PROMPT)


@pytest.mark.parametrize("kind", ["BAS", "PDS", "TS"])
def test_golden_spec_doc_systems(kind):
    # The un-voiced (no persona) BA/SA document systems — the exact sections
    # each initiation document must contain.
    from app import specs
    title, role, sections = specs.DOC_SPECS[kind]
    _check(f"system_spec_{kind}.txt",
           specs._DOC_SYSTEM.format(kind=kind, title=title, role=role,
                                    sections=sections, voice=""))


def test_golden_blueprint_and_breakdown_systems():
    from app import specs
    _check("system_blueprint.txt", specs.BLUEPRINT_SYSTEM)
    _check("system_sprint_plan.txt", specs.SPRINT_PLAN_SYSTEM)
    _check("system_po_doc_review.txt", specs.PO_DOC_REVIEW_SYSTEM)


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
