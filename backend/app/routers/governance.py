"""V3 Step 1 governance routes — lifecycle, requirements, baselines,
change requests (spec §4/§10/§11; AF3-001/005/007) — plus V3 Step 2 spec
pipeline documents (BAS/PDS/TS/BLUEPRINT/SPRINT-PLAN) for chat links."""
from __future__ import annotations

import sys

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from .. import governance

router = APIRouter(prefix="/api/v1", tags=["governance"])


def _project_or_404(pid: str):
    from ..db import query_one
    if not query_one("SELECT id FROM projects WHERE id = ?", (pid,)):
        raise HTTPException(404, "Project not found")


class TransitionIn(BaseModel):
    to: str
    reason: str = ""


class RequirementIn(BaseModel):
    title: str
    content: str = ""
    source_type: str = "text"
    source_path: str = ""


class BaselineIn(BaseModel):
    kind: str = "requirement"


class DecisionIn(BaseModel):
    decision: str          # approve | reject | request_revision
    notes: str = ""


class CRDecisionIn(BaseModel):
    decision: str          # incorporate | decline
    notes: str = ""


@router.get("/projects/{pid}/lifecycle")
def get_lifecycle(pid: str):
    _project_or_404(pid)
    view = governance.lifecycle_view(pid)
    if "error" in view:
        raise HTTPException(404, view["error"])
    return view


@router.post("/projects/{pid}/lifecycle/transition")
def transition(pid: str, body: TransitionIn):
    _project_or_404(pid)
    ok, err = governance.transition_project(pid, body.to, body.reason, actor="user")
    if not ok:
        raise HTTPException(409, err)
    return {"state": governance.current_state(pid)}


@router.post("/projects/{pid}/requirements")
def add_requirement(pid: str, body: RequirementIn):
    _project_or_404(pid)
    if not body.title.strip():
        raise HTTPException(400, "Requirement title is required")
    row = governance.create_requirement(pid, body.title.strip(), body.content,
                                        body.source_type, body.source_path)
    return {k: row[k] for k in ("id", "title", "version", "status", "source_type")}


@router.post("/projects/{pid}/baseline")
def propose_baseline(pid: str, body: BaselineIn):
    _project_or_404(pid)
    result = governance.propose_baseline(pid, body.kind, actor="user")
    if result.get("error"):
        raise HTTPException(409, result["error"])
    b = result["baseline"]
    return {"id": b["id"], "code": b["code"], "kind": b["kind"], "status": b["status"],
            "state": governance.current_state(pid)}


@router.post("/projects/{pid}/baseline/{bid}/decision")
def baseline_decision(pid: str, bid: str, body: DecisionIn):
    _project_or_404(pid)
    result = governance.decide_baseline(pid, bid, body.decision, body.notes,
                                        actor="user")
    if result.get("error") or not result.get("ok"):
        raise HTTPException(409, result.get("error") or "transition failed")
    return {"state": result["state"]}


@router.get("/projects/{pid}/documents")
def list_documents(pid: str):
    _project_or_404(pid)
    from .. import specs
    return [{"kind": d["kind"], "title": d["title"], "version": d["version"],
             "status": d["status"], "updated_at": d["updated_at"]}
            for d in specs.docs(pid)]


@router.get("/projects/{pid}/documents/{kind}", response_class=PlainTextResponse)
def get_document(pid: str, kind: str):
    _project_or_404(pid)
    from .. import specs
    doc = specs.latest_doc(pid, kind)
    if not doc:
        raise HTTPException(404, f"No {kind.upper()} document for this project yet")
    return doc["content_md"]


_REVEAL_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Revealed</title>"
    "<body style='font:14px system-ui;margin:2rem'>📂 {msg}"
    "<script>setTimeout(function(){{window.close()}},1200)</script></body>"
)


@router.get("/projects/{pid}/documents/{kind}/reveal", response_class=HTMLResponse)
def reveal_document(pid: str, kind: str):
    """Open the project's docs folder in the OS file manager, selecting the
    authored document. Local-only convenience for the desktop app."""
    import os
    import re
    import subprocess
    _project_or_404(pid)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", kind):
        raise HTTPException(400, "Invalid document name")
    from ..db import query_one
    row = query_one("SELECT workspace_path FROM projects WHERE id = ?", (pid,))
    ws = (row or {}).get("workspace_path")
    if not ws:
        return HTMLResponse(_REVEAL_PAGE.format(
            msg="This project has no workspace path set, so there is no folder to open."))
    docs_dir = os.path.join(ws, "docs")
    target = os.path.join(docs_dir, f"{kind}.md")
    try:
        if not os.path.isdir(docs_dir):
            os.makedirs(docs_dir, exist_ok=True)
        system = os.name
        if system == "nt":
            if os.path.exists(target):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
            else:
                os.startfile(docs_dir)  # noqa: S606
        elif system == "posix":
            if sys.platform == "darwin" and os.path.exists(target):
                subprocess.Popen(["open", "-R", target])
            else:
                subprocess.Popen(["xdg-open", docs_dir])
        msg = f"Opened <b>{docs_dir}</b>" + (f" and selected {kind}.md"
                                             if os.path.exists(target) else "")
    except OSError as exc:
        raise HTTPException(500, f"Could not open the folder: {exc}")
    return HTMLResponse(_REVEAL_PAGE.format(msg=msg))


@router.get("/projects/{pid}/change_requests")
def change_requests(pid: str):
    _project_or_404(pid)
    return governance.list_change_requests(pid)


@router.post("/projects/{pid}/change_requests/{cid}/decision")
def change_request_decision(pid: str, cid: str, body: CRDecisionIn):
    _project_or_404(pid)
    result = governance.decide_change_request(pid, cid, body.decision, body.notes,
                                              actor="user")
    if result.get("error"):
        raise HTTPException(409, result["error"])
    return {"state": result["state"]}


# ------------------------------------------------------------------ design phase

class DesignDecisionIn(BaseModel):
    decision: str
    comments: str = ""


@router.get("/projects/{pid}/design")
def design_state(pid: str):
    _project_or_404(pid)
    from .. import design
    return design.state(pid)


@router.post("/projects/{pid}/design/start")
def design_start(pid: str):
    _project_or_404(pid)
    from .. import design, flows
    from ..db import query_one
    project = query_one("SELECT * FROM projects WHERE id = ?", (pid,))
    flow = flows.flow_for_project(project)
    return {"message": design.start_run(pid, flow)}


@router.post("/projects/{pid}/design/decide")
def design_decide(pid: str, body: DesignDecisionIn):
    _project_or_404(pid)
    from .. import design
    if body.decision == "approve":
        return {"message": design.approve_design(pid)}
    if body.decision == "reject":
        return {"message": design.reject_design(pid, body.comments)}
    raise HTTPException(400, "decision must be approve | reject")
