"""Project Flow template routes — CRUD for reusable SDLC definitions."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import flows

router = APIRouter(prefix="/api/v1", tags=["flows"])


class FlowIn(BaseModel):
    name: str
    description: str = ""
    stages: list[str] = list(flows.DEFAULT_STAGES)
    team: list[dict] = []
    design: list[dict] = []
    docs_gate: int = 0
    po_enabled: int = 0
    is_default: int = 0


@router.get("/flows")
def list_flows():
    return flows.list_flows()


@router.get("/flows/stage_kinds")
def stage_kinds():
    return [{"key": k, "kind": v[0], "role": v[1], "label": flows.STAGE_LABEL[k]}
            for k, v in flows.STAGE_DEFS.items()]


@router.get("/flows/process_catalog")
def process_catalog():
    from .. import design
    return design.catalog()


@router.post("/flows", status_code=201)
def create_flow(body: FlowIn):
    try:
        return flows.create_flow(body.name, body.description, body.stages,
                                 body.team, body.docs_gate, body.po_enabled,
                                 body.is_default, body.design)
    except flows.FlowError as exc:
        raise HTTPException(400, str(exc))


@router.get("/flows/{flow_id}")
def get_flow(flow_id: str):
    flow = flows.get_flow(flow_id)
    if not flow:
        raise HTTPException(404, "Flow not found")
    return flow


@router.put("/flows/{flow_id}")
def update_flow(flow_id: str, body: FlowIn):
    try:
        return flows.update_flow(flow_id, name=body.name,
                                 description=body.description, stages=body.stages,
                                 team=body.team, docs_gate=body.docs_gate,
                                 po_enabled=body.po_enabled, is_default=body.is_default,
                                 design=body.design)
    except flows.FlowError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/flows/{flow_id}")
def delete_flow(flow_id: str):
    try:
        flows.delete_flow(flow_id)
    except flows.FlowError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True}
