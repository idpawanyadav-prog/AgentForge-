"""Pydantic models for update/PATCH endpoints in the AgentForge API.

Each model uses ``Optional`` fields so partial updates are accepted
— only the fields the caller sends are written to the database.
"""
from typing import Optional

from pydantic import BaseModel


class GatewayUpdate(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_type: Optional[str] = None
    status: Optional[str] = None


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    role_id: Optional[str] = None
    persona_id: Optional[str] = None
    model_binding_id: Optional[str] = None
    lifecycle_state: Optional[str] = None


class RoleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    active: Optional[bool] = None


class PersonaUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None
    constraints_text: Optional[str] = None
    active: Optional[bool] = None


class SkillUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    active: Optional[bool] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    goal: Optional[str] = None
    description: Optional[str] = None
    technology_stack: Optional[str] = None
    repository_url: Optional[str] = None
    workspace_path: Optional[str] = None
    default_gateway_id: Optional[str] = None
    team_id: Optional[str] = None
    status: Optional[str] = None
    po_enabled: Optional[bool] = None


class SprintUpdate(BaseModel):
    name: Optional[str] = None
    goal: Optional[str] = None
    capacity: Optional[int] = None
    status: Optional[str] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None


class BacklogUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[int] = None
    acceptance_criteria: Optional[str] = None
    story_points: Optional[int] = None
    status: Optional[str] = None


class BotConfigUpdate(BaseModel):
    gateway_id: Optional[str] = None
    model_id: Optional[str] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    story_points: Optional[int] = None
    priority: Optional[int] = None
    sprint_id: Optional[str] = None
    backlog_item_id: Optional[str] = None
    assigned_agent_id: Optional[str] = None
    status: Optional[str] = None
    blocked_reason: Optional[str] = None


class TeamUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    agent_ids: Optional[list[str]] = None


class InstructionUpdate(BaseModel):
    filename: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    active: Optional[bool] = None
