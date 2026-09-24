"""V3 Step 1 governance routes — lifecycle, requirements, baselines,
change requests (spec §4/§10/§11; AF3-001/005/007)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
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
