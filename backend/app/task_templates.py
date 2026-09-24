"""Task template system (AGENT_DEV_SPEEDUP 2.1).

Templates live in backend/app/templates/*.json and encode the recurring
shape of a task kind: title prefix, description body, Given/When/Then
acceptance criteria, default story points and the role that usually does
the work. {placeholders} are filled from template_variables at creation
time so the PO and the agents always see consistent, complete tasks.
"""
import json
import os
import re

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")


def list_templates() -> list[dict]:
    out = []
    try:
        names = sorted(f for f in os.listdir(TEMPLATE_DIR) if f.endswith(".json"))
    except OSError:
        return out
    for name in names:
        t = _load(name[:-5])
        if t:
            out.append(t)
    return out


def get_template(template_id: str) -> dict | None:
    safe = re.sub(r"[^a-z0-9_-]", "", (template_id or "").lower())
    return _load(safe) if safe else None


def _load(stem: str) -> dict | None:
    path = os.path.join(TEMPLATE_DIR, stem + ".json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("id", stem)
        return data
    except (OSError, ValueError):
        return None


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _fill(text: str, variables: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: str((variables or {}).get(m.group(1))
                                          or f"[{m.group(1)}?]"), text or "")


def apply_template(template_id: str, variables: dict) -> dict | None:
    """Return concrete task fields for a template, or None if unknown."""
    t = get_template(template_id)
    if not t:
        return None
    return {
        "template_id": t["id"],
        "title": _fill(t.get("title", ""), variables),
        "description": _fill(t.get("description", ""), variables),
        "acceptance_criteria": _fill(t.get("acceptance_criteria", ""), variables),
        "story_points": t.get("story_points", 3),
        "priority": t.get("priority", 2),
        "suggested_role": t.get("suggested_role", ""),
        "missing_variables": sorted(
            {m.group(1) for field in ("title", "description", "acceptance_criteria")
             for m in _PLACEHOLDER.finditer(t.get(field, ""))
             if not (variables or {}).get(m.group(1))}),
    }
