"""V3 Step 2 — project-initiation spec pipeline.

Flow (owner-approved design, one approval gate):

1. `analyze project` in the control chat starts the pipeline: the Business
   Analyst drafts the BAS (Business Analysis Specification) and the PDS
   (Product Design Specification); the Solution Architect drafts the TS
   (Technical Specification). Documents land in `project_documents`
   (versioned, append-only), are mirrored to `<workspace>/docs/*.md`, and
   are ingested as requirements so the existing RB baseline machinery
   freezes them.
2. Approval gate (the ONLY gate): if a Product Owner agent is assigned to
   the project's team it reviews the documents and approves or requests
   revision automatically; otherwise the human approves from the same chat
   with `approve` / `proceed` / `ok` (or `reject` / `revise ...`).
3. Once the requirement baseline is approved the pipeline continues
   automatically: the SA authors the whole-project blueprint (ADRs, file
   contracts, interface contracts, component registry — the 009 tables),
   the AB baseline is recorded as covered by the RB approval (single gate),
   and the blueprint is sliced into sprints such that every sprint leaves a
   RUNNING app plus its own increment.
4. Per-task delivery is unchanged (dev -> SA review -> BA review -> QA);
   devs and the SA reviewer now see the written file/interface contracts.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading

from . import budget, codegen, governance, po
from .db import (audit, emit_event, execute, insert, new_id, now, query,
                 query_one, update)

logger = logging.getLogger(__name__)

_MAX_DOC_CHARS = 30000
_MAX_CONTRACT_BLOCK = 2400

# kind -> (title, author role, spec sections)
DOC_SPECS = {
    "BAS": ("Business Analysis Specification", "Business Analyst", """
1. Business context & objectives
2. Stakeholders & users (actors and what each needs)
3. Functional requirements — numbered FR-1, FR-2, ... each with concrete
   Given/When/Then acceptance criteria
4. Non-functional requirements — numbered NFR-1, ... (performance, reliability,
   usability, security where relevant)
5. Business rules & constraints
6. Explicitly out of scope
7. Assumptions & open questions"""),
    "PDS": ("Product Design Specification", "Business Analyst", """
1. Product vision & target users (short)
2. User stories — numbered US-1, ... each INVEST-shaped, with priority
   (MoSCoW) and a story-point estimate
3. Screens / flows — every user-facing surface, what it shows, what the user
   can do, empty/error/loading states
4. Definition of done per story — browser-verifiable behavior statements
5. Release priorities — which stories are must-have for a first usable build"""),
    "TS": ("Technical Specification", "Solution Architect", """
1. Chosen stack & versions (respect the project's stated technology stack)
2. System architecture overview (components, data flow, where state lives)
3. External services, APIs and integrations (keys/env vars they need)
4. Data model / persistence decisions
5. Project layout conventions (directory structure, naming, entry points)
6. Error handling, logging and configuration strategy
7. Quality bar: test strategy (unit + end-to-end), build & run commands
8. Technical risks & mitigations"""),
    "ARCH": ("Backend Architecture", "Solution Architect", """
1. Service / module decomposition and responsibilities
2. Data stores, schemas and state management
3. API surface: endpoints, contracts, auth model
4. Integration points and external dependencies
5. Concurrency, scaling and reliability considerations
6. Deployment topology and environments"""),
    "DEVPLAN": ("Development Approach", "Senior Developer", """
1. Implementation strategy over the approved architecture
2. Build order and milestones (what is wired up first to get a running skeleton)
3. Key libraries/tooling and how they are used
4. Coding conventions, branching and review practices
5. Definition-of-done checklist per feature
6. Known hard problems and how to tackle them"""),
    "IMPLPLAN": ("Implementation Plan", "Senior Developer", """
1. Concrete work breakdown mapped to the requirement items
2. Files/modules to create or change per task
3. Interfaces and data shapes each piece exposes
4. Sequencing and dependencies between implementation tasks
5. Effort / risk notes per work item
6. Rollout and integration checkpoints"""),
    "TESTPLAN": ("Test Strategy", "QA Engineer", """
1. Testing scope and quality goals
2. Test levels: unit, integration, end-to-end — what each covers
3. Key scenarios and acceptance tests tied to requirements
4. Test tooling, environments and data
5. Entry/exit criteria and release gates
6. Automation coverage targets"""),
}

_DOC_SYSTEM = """You are the {role} of an autonomous software delivery team, writing the \
{kind} — {title} — for a real project. Write ONLY the markdown document body (start with \
`# {title} — <project name>`), no code fences around the whole document, no preamble or \
afterword. Base every statement on the project brief below; where the brief is silent, \
make a sensible, minimal assumption and record it under Assumptions. Keep it complete but \
tight: a developer must be able to execute from it without inventing scope.\
{voice}

Required sections:
{sections}"""

BLUEPRINT_SYSTEM = """You are the Solution Architect designing the complete technical \
blueprint of a real project so a team of developer agents can execute it sprint by sprint. \
You are given the project's approved specifications (BAS / PDS / TS excerpts). Design the \
SMALLEST coherent architecture that satisfies them on the stated stack.

Respond with ONLY a JSON object (no markdown fences):
{"decisions": [{"title": str, "decision": str, "consequences": str}],
 "components": [{"code": "C-1", "name": str, "path": str, "purpose": str, "owner_role": str}],
 "file_contracts": [{"path": str, "module": str, "owner_role": str, "purpose": str,
                     "must_implement": [str], "allowed_deps": [str], "forbidden": [str],
                     "technical_ac": str}],
 "interface_contracts": [{"name": str, "module": str, "signature": str}]}

Rules:
- 3-6 architecture decisions (ADR-style), conventional for the stack.
- file_contracts: EVERY file the project needs, with what it must expose,
  which imports it may use, and what is forbidden there. Paths are workspace-relative.
- interface_contracts: the key functions/routes/commands between modules.
- Keep the file count realistic for the scope — this is a working app, not an enterprise repo."""

SPRINT_PLAN_SYSTEM = """You are the Solution Architect breaking an approved blueprint into \
ordered delivery sprints for an autonomous dev team. Respond with ONLY a JSON object \
(no markdown fences):
{"sprints": [{"name": str, "goal": str,
   "tasks": [{"title": str, "description": str, "acceptance_criteria": str,
              "points": int, "priority": int, "files": [str]}]}]}

Hard rules:
- Sprint 1 must scaffold a RUNNING application (bootable entry point + health/smoke test),
  even if minimal.
- Every sprint ends with the app still RUNNING and passing its tests, plus that sprint's
  new working feature. Never split in a way that leaves a non-bootable app at a sprint end.
- 2-5 sprints total, ordered by dependency; 3-8 tasks each, 1-8 points, priority 1-3.
- Each task's `files` lists the blueprint file contracts it creates or touches; acceptance
  criteria are concrete and testable (Given/When/Then)."""

PO_DOC_REVIEW_SYSTEM = """You are the Product Owner agent reviewing the initiation documents \
(BAS / PDS / TS) of a project on the owner's behalf. Judge: do these documents describe a \
coherent, right-sized, buildable product for the stated goal? Reject only for material \
problems (contradictions, missing core requirements, scope wildly beyond the goal) — not \
style nits. Respond with ONLY a JSON object (no markdown fences):
{"verdict": "approve" | "request_revision", "notes": "<decision rationale; concrete fix \
list when requesting revision>"}"""

# ------------------------------------------------------------------ storage


def is_pipeline_project(project_id: str) -> bool:
    return bool(query_one("SELECT id FROM project_documents WHERE project_id = ? LIMIT 1",
                          (project_id,)))


def docs(project_id: str) -> list:
    return query("SELECT * FROM project_documents WHERE project_id = ? "
                 "ORDER BY kind, version", (project_id,))


def latest_doc(project_id: str, kind: str):
    return query_one("SELECT * FROM project_documents WHERE project_id = ? AND kind = ? "
                     "ORDER BY version DESC LIMIT 1", (project_id, kind.upper()))


EXTRA_DOC_TITLES = {"BLUEPRINT": "Solution Blueprint", "SPRINT-PLAN": "Sprint Plan"}


def save_doc(project_id: str, kind: str, content_md: str, review_notes: str = "",
             status: str = "pending_approval", actor: str = "agent") -> dict:
    kind = kind.upper()
    title = EXTRA_DOC_TITLES.get(kind) or DOC_SPECS.get(kind, (f"{kind} document",))[0]
    role = DOC_SPECS.get(kind, ("", ""))[1]
    prev = latest_doc(project_id, kind)
    version = (prev["version"] if prev else 0) + 1
    if prev:
        execute("UPDATE project_documents SET status = 'superseded', updated_at = ? "
                "WHERE id = ?", (now(), prev["id"]))
    ts = now()
    did = new_id()
    insert("project_documents", {
        "id": did, "project_id": project_id, "kind": kind,
        "title": f"{title} — v{version}", "content_md": (content_md or "")[:_MAX_DOC_CHARS],
        "version": version, "status": status, "review_notes": review_notes[:2000],
        "created_at": ts, "updated_at": ts})
    emit_event(project_id, "document.saved", {"kind": kind, "version": version})
    audit("document_saved", "project", project_id,
          f"{kind} v{version} authored by {role or actor}", actor="agent")
    _write_workspace_doc(project_id, kind)
    return query_one("SELECT * FROM project_documents WHERE id = ?", (did,))


def _write_workspace_doc(project_id: str, kind: str):
    project = query_one("SELECT workspace_path FROM projects WHERE id = ?", (project_id,))
    doc = latest_doc(project_id, kind)
    if not project or not project["workspace_path"] or not doc:
        return
    docs_dir = os.path.join(project["workspace_path"], "docs")
    try:
        os.makedirs(docs_dir, exist_ok=True)
        with open(os.path.join(docs_dir, f"{kind}.md"), "w", encoding="utf-8") as fh:
            fh.write(doc["content_md"])
    except OSError:
        logger.warning("could not mirror %s doc to workspace for %s", kind, project_id)


# ------------------------------------------------------------------ LLM plumbing


class SpecsError(RuntimeError):
    pass


def _role_voice(project_id: str, role_name: str) -> str:
    """Persona + role instructions of the project's agent holding this role,
    so authored documents carry that agent's established voice."""
    row = query_one(
        "SELECT a.role_id, a.persona_id FROM agents a "
        "JOIN roles r ON r.id = a.role_id "
        "JOIN team_agents ta ON ta.agent_id = a.id AND ta.active = 1 "
        "JOIN teams t ON t.id = ta.team_id JOIN projects p ON p.team_id = t.id "
        "WHERE p.id = ? AND lower(r.name) = lower(?) LIMIT 1", (project_id, role_name))
    if not row:
        return ""
    persona = query_one("SELECT * FROM personas WHERE id = ?", (row["persona_id"],)) \
        if row["persona_id"] else None
    return codegen._persona_block(persona) + codegen._role_instructions_block(row["role_id"])


def _llm(project_id: str, system: str, user: str, max_tokens: int = 4000,
         label: str = "specs") -> str:
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise SpecsError("Project not found")
    ok, reason = budget.may_spend(project_id)
    if not ok:
        raise SpecsError(reason)
    gw, model = codegen.resolve_llm(project)
    if not (gw and model):
        raise SpecsError("No LLM gateway/model configured for this project — the pipeline "
                         "cannot draft documents. Set the project's gateway+model first.")
    resp = codegen.call_llm(gw, model, system, user, max_tokens=max_tokens,
                            trace_label=label)
    codegen._charge_llm_call(project, model, resp)
    if resp.get("dry_run"):
        raise SpecsError("DRY_RUN mode is on — document authoring needs a real LLM reply. "
                         "Turn DRY_RUN off and run `analyze project` again.")
    return str(resp.get("text") or "")


def _brief_block(project_id: str) -> str:
    p = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    return (f"PROJECT: {p['name']}\nGOAL: {p['goal'] or 'n/a'}\n"
            f"DESCRIPTION: {(p['description'] or 'n/a')[:2000]}\n"
            f"TECH STACK: {p['technology_stack'] or 'you may choose a sensible default'}\n"
            f"REPOSITORY: {p['repository_url'] or 'none (greenfield)'}")


def _strip_fences(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```(?:markdown|md)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def author_doc(project_id: str, kind: str, review_notes: str = "") -> dict:
    title, role, sections = DOC_SPECS[kind]
    system = _DOC_SYSTEM.format(kind=kind, title=title, role=role,
                                sections=sections, voice=_role_voice(project_id, role))
    user = _brief_block(project_id)
    if review_notes:
        user += ("\n\nThe Product Owner returned this document for revision — address every "
                 f"point:\n{review_notes[:2000]}")
    text = _strip_fences(_llm(project_id, system, user, max_tokens=4000,
                              label=f"doc:{kind}"))
    if len(text) < 200:
        raise SpecsError(f"{kind} came back too short to be a real document "
                         f"({len(text)} chars) — run `analyze project` again.")
    return save_doc(project_id, kind, text, review_notes=review_notes)


# ------------------------------------------------------------------ chat narration


def _post(project_id: str, text: str, conversation_id: str | None = None):
    if conversation_id:
        insert("messages", {"id": new_id(), "conversation_id": conversation_id,
                            "role": "assistant", "content": text, "created_at": now()})
        update("conversations", conversation_id, {"updated_at": now()})
        emit_event(project_id, "chat.message", {"conversation_id": conversation_id})
        return
    po.po_narrate(project_id, text)


def _doc_links(project_id: str, kinds: list[str]) -> str:
    return "\n".join(
        f"- **{k}**: [📂 docs/{k}.md](/api/v1/projects/{project_id}/documents/{k}/reveal)"
        for k in kinds)


def pipeline_brief(project_id: str) -> str:
    """Lines appended to the copilot context brief so routing knows the stage."""
    d = docs(project_id)
    if not d:
        return ("Spec pipeline: not started — say `analyze project` to have the BA draft "
                "BAS/PDS and the SA draft TS, then review.")
    state = governance.current_state(project_id)
    by_kind = {}
    for row in d:
        by_kind[row["kind"]] = row
    ready = [k for k in ("BAS", "PDS", "TS") if k in by_kind]
    lines = [f"Spec pipeline: state '{state}'; latest docs: "
             + (", ".join(f"{k} v{by_kind[k]['version']} ({by_kind[k]['status']})"
                          for k in ready) or "none")]
    pending = pending_requirement_baseline(project_id)
    if pending:
        lines.append(f"Pending baseline {pending['code']} — the user approves with "
                     "`approve`/`proceed` or rejects with `reject` / `revise <notes>`.")
    assigned_po = po.po_agent_for_project(project_id)
    lines.append("Reviewer: " + (f"PO agent '{assigned_po['name']}' reviews and proceeds "
                                 "automatically" if assigned_po else
                                 "the human owner (approve in this chat)"))
    if state == governance.READY_PLANNING:
        lines.append("Sprints are planned from the approved blueprint — `start sprint` runs them.")
    return "\n".join(lines)


# ------------------------------------------------------------------ approval helpers


def pending_requirement_baseline(project_id: str):
    return query_one("SELECT * FROM project_baselines WHERE project_id = ? AND kind = "
                     "'requirement' AND status = 'pending_approval' "
                     "ORDER BY created_at DESC LIMIT 1", (project_id,))


def decide_pending_baseline(project_id: str, decision: str, notes: str = "",
                            actor: str = "user") -> dict:
    """Chat-facing wrapper over governance.decide_baseline for the pending
    requirement baseline (the single approval gate)."""
    b = pending_requirement_baseline(project_id)
    if not b:
        return {"error": "There is no document baseline awaiting approval right now."}
    result = governance.decide_baseline(project_id, b["id"], decision, notes, actor=actor)
    result["code"] = b["code"]
    return result


def chat_approve(project_id: str) -> str:
    """Human approval of the pending documents from the control chat. The
    governance hook then runs blueprint + sprint breakdown in the
    background."""
    r = decide_pending_baseline(project_id, "approve", actor="user")
    if r.get("error") or not r.get("ok"):
        return f"Could not approve {r.get('code', 'the baseline')}: " \
               f"{r.get('error') or 'transition failed'}"
    return (f"✅ **{r['code']} approved** — the SA is now authoring the project blueprint "
            "and the sprint breakdown (single approval gate). Watch this chat for the "
            "blueprint and sprint links.")


def chat_reject(project_id: str, notes: str = "") -> str:
    r = decide_pending_baseline(project_id, "reject", notes, actor="user")
    if r.get("error") or not r.get("ok"):
        return f"Could not reject {r.get('code', 'the baseline')}: " \
               f"{r.get('error') or 'transition failed'}"
    return (f"❌ **{r['code']} rejected** — a change request was opened and the project is "
            "parked in 'Change Requested'. Say `analyze project` to re-draft the documents.")


def chat_revise(project_id: str, notes: str) -> str:
    r = decide_pending_baseline(project_id, "request_revision", notes, actor="user")
    if r.get("error") or not r.get("ok"):
        return f"Could not request revision of {r.get('code', 'the baseline')}: " \
               f"{r.get('error') or 'transition failed'}"
    if governance.current_state(project_id) == governance.CHANGE_REQUESTED:
        governance.transition_project(project_id, governance.REQ_ANALYSIS,
                                      reason="manual document revision in progress",
                                      actor="user")
    if not _claim(project_id):
        return (f"Revision recorded for {r['code']}, but the pipeline is busy — "
                "say `analyze project` again once it is idle.")
    threading.Thread(target=_documents_worker, args=(project_id, None),
                     kwargs={"review_notes": notes or "General polish requested."},
                     daemon=True, name=f"specs-rev-{project_id[:8]}").start()
    return (f"🔁 Re-drafting BAS/PDS/TS with your feedback: “{(notes or '')[:160]}” — "
            "the new document links will land here shortly.")


# ------------------------------------------------------------------ pipeline threads

_lock = threading.Lock()
_running: dict[str, int] = {}   # project_id -> thread ident owning the pipeline


def _claim(project_id: str) -> bool:
    with _lock:
        if project_id in _running:
            return False
        _running[project_id] = threading.get_ident()
        return True


def _release(project_id: str):
    with _lock:
        _running.pop(project_id, None)


def start_pipeline(project_id: str, conversation_id: str | None = None) -> str:
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        return "Project not found."
    state = governance.current_state(project_id)
    if state in (governance.ACTIVE_DEV, governance.FINAL_VALIDATION, governance.COMPLETED):
        return (f"Project is already in '{state}' — the initiation pipeline does not "
                "re-run there. Use change requests / new sprints for new scope.")
    if not _claim(project_id):
        return "The specification pipeline is already running for this project — I'll post here when each step finishes."
    if pending_requirement_baseline(project_id):
        _release(project_id)
        return ("The drafted documents are still awaiting approval — reply `approve`, "
                "`reject` or `revise <notes>` first, or supersede them with a new plan.")
    # This flow implies the lifecycle gate: execution must wait for approval.
    if not project["governance_enabled"]:
        update("projects", project_id, {"governance_enabled": 1, "updated_at": now()})
        audit("governance_enabled", "project", project_id,
              "Governance auto-enabled by the spec pipeline (execution waits for approval)",
              actor="agent")
    if state == governance.DRAFT:
        governance.transition_project(project_id, governance.REQ_ANALYSIS,
                                      reason="BA/SA document drafting started",
                                      actor="agent")
    threading.Thread(target=_documents_worker, args=(project_id, conversation_id),
                     daemon=True, name=f"specs-{project_id[:8]}").start()
    return ("Starting the initiation pipeline: the Business Analyst will draft the **BAS** "
            "(Business Analysis Spec) and **PDS** (Product Design Spec), the Solution "
            "Architect the **TS** (Technical Spec). Drafting takes a few minutes — I'll post "
            "the document links here as they land, then the approval step follows.")


def _documents_worker(project_id: str, conversation_id: str | None,
                      review_notes: str = "", cycle: int = 0):
    try:
        for kind in ("BAS", "PDS", "TS"):
            _post(project_id, f"✍️ Drafting **{kind}** — {DOC_SPECS[kind][1]} …",
                  conversation_id)
            doc = author_doc(project_id, kind, review_notes)
            # Same title each pass → create_requirement versions + supersedes
            # the old one automatically, so the RB snapshot stays clean.
            governance.create_requirement(project_id, kind, doc["content_md"],
                                          source_type="doc", source_path=f"docs/{kind}.md",
                                          actor="agent")
        res = governance.propose_baseline(project_id, "requirement", actor="agent")
        if res.get("error"):
            _post(project_id, f"⚠️ Documents drafted but the baseline could not be "
                              f"proposed: {res['error']}", conversation_id)
            return
        code = res["baseline"]["code"]
        bid = res["baseline"]["id"]
        _post(project_id,
              f"📄 **{code}** — the initiation documents are ready for review:\n"
              + _doc_links(project_id, ["BAS", "PDS", "TS"])
              + "\n\n" + _reviewer_line(project_id), conversation_id)
        assigned_po = po.po_agent_for_project(project_id)
        if not assigned_po:
            return  # human path: waiting for `approve` / `reject` / `revise`
        _post(project_id, f"👑 {assigned_po['name']} (Product Owner) is reviewing the "
                          "documents now …", conversation_id)
        verdict = _po_review(project_id)
        if verdict["verdict"] == "approve":
            _post(project_id, f"👑 **{assigned_po['name']} approved {code}** — "
                              f"notes: {verdict['notes'][:400]}\n"
                              "Continuing with the architecture blueprint and sprint "
                              "breakdown (single approval gate) …", conversation_id)
            governance.decide_baseline(project_id, bid, "approve",
                                       verdict["notes"][:600], actor="product-owner")
            return  # decide_baseline hooks into on_requirements_approved
        if cycle >= 1:
            _post(project_id, f"👑 {assigned_po['name']} requested revision twice — pausing "
                              "for your judgement. Reply `approve` to accept the documents "
                              f"as they are, or `revise <notes>` to steer another pass.",
                  conversation_id)
            return
        _post(project_id, f"👑 **{assigned_po['name']} requested revision** of {code}:\n"
                          + "\n".join(f"- {ln.strip()}" for ln in
                                      re.split(r"[.;\n]+", verdict["notes"][:800])
                                      if ln.strip())
                          + "\n\nRe-drafting the documents with the PO's feedback …",
                  conversation_id)
        governance.decide_baseline(project_id, bid, "request_revision",
                                   verdict["notes"][:600], actor="product-owner")
        if governance.current_state(project_id) == governance.CHANGE_REQUESTED:
            governance.transition_project(project_id, governance.REQ_ANALYSIS,
                                          reason="revised documents in progress",
                                          actor="agent")
        _documents_worker(project_id, conversation_id,
                          review_notes=verdict["notes"], cycle=cycle + 1)
    except Exception as exc:
        logger.exception("spec documents pipeline failed for %s", project_id)
        _post(project_id, f"⚠️ The document pipeline stopped: {str(exc)[:300]}\n"
                          "Fix the cause, then say `analyze project` again.", conversation_id)
    finally:
        if cycle == 0:
            _release(project_id)


def _reviewer_line(project_id: str) -> str:
    assigned_po = po.po_agent_for_project(project_id)
    if assigned_po:
        return (f"{assigned_po['name']} (Product Owner) is assigned, so the review runs "
                "automatically. Reply `approve` now to decide it yourself.")
    return "Reply `approve` (or `proceed` / `ok`) to unlock blueprint + sprint planning, or `reject` / `revise <notes>`."


def _po_review(project_id: str) -> dict:
    summary = "\n\n".join(
        f"=== {k} (latest v{n['version']}) ===\n{n['content_md'][:6000]}"
        for k in ("BAS", "PDS", "TS") if (n := latest_doc(project_id, k)))
    text = _llm(project_id,
                PO_DOC_REVIEW_SYSTEM + _role_voice(project_id, po.PO_ROLE),
                _brief_block(project_id) + "\n\n" + summary,
                max_tokens=700, label="po:doc-review")
    data = codegen._extract_object(text)
    verdict = str(data.get("verdict") or "").lower()
    if verdict not in ("approve", "request_revision"):
        verdict = "approve"  # unusable PO output must not stall the one gate forever
    return {"verdict": verdict, "notes": str(data.get("notes") or "")[:1000]}


# ------------------------------------------------------------------ post-approval: blueprint + sprints


def on_requirements_approved(project_id: str):
    """Hook called by governance when the requirement baseline is approved.
    Pipeline-thread approvals continue inline; human/UI approvals run the
    rest of the pipeline on a fresh background thread."""
    if not is_pipeline_project(project_id):
        return
    if _running.get(project_id) == threading.get_ident():
        continue_after_approval(project_id, None)
        return
    if not _claim(project_id):
        return
    threading.Thread(target=_continuation_worker, args=(project_id,),
                     daemon=True, name=f"specs-cont-{project_id[:8]}").start()


def _continuation_worker(project_id: str):
    try:
        continue_after_approval(project_id, None)
    except Exception:
        # A blueprint/breakdown LLM failure used to kill this daemon thread
        # silently, stranding the project in Scaffolding with no message and
        # no recovery. Surface it and point at a way forward instead.
        logger.exception("post-approval blueprint/breakdown failed for %s", project_id)
        _post(project_id, "⚠️ The blueprint / sprint-breakdown step stopped before it "
                          "finished, so the project is still in Scaffolding. Your approved "
                          "requirement baseline is safe — nothing was lost. Say `start sprint` "
                          "to run a sprint you create, or `analyze project` to re-run the "
                          "blueprint and breakdown.", None)
    finally:
        _release(project_id)


def continue_after_approval(project_id: str, conversation_id: str | None):
    """Runs after the single approval gate: SA blueprint (009 tables) → AB
    baseline (recorded, covered by the RB approval — one gate by design) →
    blueprint-driven sprint breakdown → Ready for Planning."""
    state = governance.current_state(project_id)
    if state == governance.APPROVED:
        governance.transition_project(project_id, governance.SCAFFOLDING,
                                      reason="blueprint authoring started", actor="agent")
    _post(project_id, "🏛️ Solution Architect is authoring the project blueprint "
                      "(ADRs, file & interface contracts) …", conversation_id)
    blueprint = author_blueprint(project_id)
    _post(project_id,
          "🏛️ **Blueprint ready** (recorded as "
          f"{blueprint['ab_code']}; your document approval already covered it — one gate):\n"
          + _doc_links(project_id, ["BLUEPRINT"])
          + f"\n- {blueprint['decisions']} architecture decisions, "
            f"{blueprint['files']} file contracts, {blueprint['interfaces']} interfaces",
          conversation_id)
    _post(project_id, "🗓️ Slicing the blueprint into runnable sprints …", conversation_id)
    plan = breakdown_sprints(project_id)
    execute("UPDATE project_documents SET status = 'approved' WHERE project_id = ? "
            "AND kind IN ('BAS','PDS','TS') AND status = 'pending_approval'", (project_id,))
    state = governance.current_state(project_id)
    if state == governance.SCAFFOLDING:
        governance.transition_project(project_id, governance.READY_PLANNING,
                                      reason="sprint breakdown completed", actor="agent")
    lines = [f"🗓️ **Sprint breakdown** from the blueprint — every sprint ends with a running "
             f"app plus its increment ({plan['sprints']} sprints, {plan['tasks']} tasks):"]
    for s in plan["sprint_lines"]:
        lines.append(f"- {s}")
    lines.append("")
    lines.append(_doc_links(project_id, ["SPRINT-PLAN"]))
    lines.append("\nSay `start sprint` to run Sprint 1 (SA → BA → QA gates unchanged).")
    _post(project_id, "\n".join(lines), conversation_id)


def _blueprint_user(project_id: str) -> str:
    parts = [_brief_block(project_id)]
    for kind in ("BAS", "PDS", "TS"):
        d = latest_doc(project_id, kind)
        if d:
            parts.append(f"=== {kind} ===\n{d['content_md'][:5000]}")
    return "\n\n".join(parts)


def author_blueprint(project_id: str) -> dict:
    """One SA run: writes architecture_decisions / interface_contracts /
    file_contracts / component_registry (009 tables), mirrors a readable
    BLUEPRINT document, and records the AB baseline as approved (the RB
    approval is the single gate)."""
    text = _llm(project_id, BLUEPRINT_SYSTEM, _blueprint_user(project_id),
                max_tokens=6000, label="blueprint")
    data = codegen._extract_object(text)
    if not isinstance(data.get("file_contracts"), list) or not data["file_contracts"]:
        raise SpecsError("The blueprint reply had no file contracts — say "
                         "`analyze project` blueprint step again (no documents change).")
    ts = now()
    existing = query_one("SELECT COUNT(*) AS n FROM architecture_decisions WHERE project_id = ?",
                         (project_id,))["n"]
    decisions = data.get("decisions") or []
    for i, d in enumerate(decisions[:8]):
        if not isinstance(d, dict):
            continue
        insert("architecture_decisions", {
            "id": new_id(), "project_id": project_id, "code": f"ADR-{existing + i + 1}",
            "title": str(d.get("title") or f"Decision {i + 1}")[:200],
            "status": "accepted", "decision": str(d.get("decision") or "")[:4000],
            "consequences": str(d.get("consequences") or "")[:2000],
            "doc_path": "docs/BLUEPRINT.md", "valid_from": ts, "valid_to": None,
            "created_at": ts, "updated_at": ts})
    # Supersede the previous blueprint generation instead of stacking actives.
    execute("UPDATE file_contracts SET status = 'superseded', updated_at = ? "
            "WHERE project_id = ? AND status = 'active'", (ts, project_id))
    execute("UPDATE interface_contracts SET status = 'superseded', updated_at = ? "
            "WHERE project_id = ? AND status = 'active'", (ts, project_id))
    execute("UPDATE component_registry SET status = 'superseded', updated_at = ? "
            "WHERE project_id = ? AND status = 'active'", (ts, project_id))
    files = data.get("file_contracts") or []
    kept_files = []
    for f in files[:40]:
        if not isinstance(f, dict):
            continue
        rel = codegen._safe_rel_path(str(f.get("path") or ""))
        if not rel:
            continue
        kept_files.append((rel, f))
        insert("file_contracts", {
            "id": new_id(), "project_id": project_id, "path": rel,
            "module": str(f.get("module") or "")[:100],
            "owner_role": str(f.get("owner_role") or "")[:60],
            "purpose": str(f.get("purpose") or "")[:600],
            "must_implement": json.dumps(_str_list(f.get("must_implement"))[:12]),
            "allowed_deps": json.dumps(_str_list(f.get("allowed_deps"))[:15]),
            "forbidden": json.dumps(_str_list(f.get("forbidden"))[:8]),
            "technical_ac": str(f.get("technical_ac") or "")[:800],
            "requirement_ids": "[]", "status": "active",
            "created_at": ts, "updated_at": ts})
    ifaces = data.get("interface_contracts") or []
    for iface in ifaces[:30]:
        if not isinstance(iface, dict):
            continue
        insert("interface_contracts", {
            "id": new_id(), "project_id": project_id,
            "name": str(iface.get("name") or "")[:200],
            "module": str(iface.get("module") or "")[:100], "revision": 1,
            "signature": str(iface.get("signature") or "")[:1500],
            "status": "active", "superseded_by": None,
            "created_at": ts, "updated_at": ts})
    for i, c in enumerate((data.get("components") or [])[:20]):
        if not isinstance(c, dict):
            continue
        insert("component_registry", {
            "id": new_id(), "project_id": project_id,
            "code": str(c.get("code") or f"C-{i + 1}")[:20],
            "name": str(c.get("name") or "")[:120],
            "path": codegen._safe_rel_path(str(c.get("path") or "")) or "",
            "owner_role": str(c.get("owner_role") or "")[:60],
            "purpose": str(c.get("purpose") or "")[:600],
            "version": "1.0", "used_by": json.dumps(_str_list(c.get("used_by"))[:10]),
            "status": "active", "created_at": ts, "updated_at": ts})
    ab = governance.record_approved_baseline(project_id, "architecture",
                                             approved_by="single-gate (RB approval)")
    save_doc(project_id, "BLUEPRINT", _blueprint_markdown(project_id),
             status="approved", actor="solution-architect")
    audit("blueprint_authored", "project", project_id,
          f"SA blueprint: {len(decisions)} ADRs, {len(kept_files)} file contracts, "
          f"{len(ifaces)} interfaces (baseline {ab['code']})", actor="agent")
    return {"decisions": len(decisions), "files": len(kept_files),
            "interfaces": len(ifaces), "ab_code": ab["code"]}


def _str_list(value) -> list:
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:160] for v in value if str(v).strip()][:20]


def _blueprint_markdown(project_id: str) -> str:
    p = query_one("SELECT name FROM projects WHERE id = ?", (project_id,))
    out = [f"# Project Blueprint — {p['name']}", ""]
    adrs = query("SELECT * FROM architecture_decisions WHERE project_id = ? "
                 "AND status = 'accepted' ORDER BY created_at", (project_id,))
    out.append("## Architecture decisions")
    for a in adrs:
        out += [f"### {a['code']} — {a['title']}", a["decision"],
                f"*Consequences:* {a['consequences']}", ""]
    out.append("## File contracts")
    for f in query("SELECT * FROM file_contracts WHERE project_id = ? AND status = 'active' "
                   "ORDER BY path", (project_id,)):
        mi = ", ".join(json.loads(f["must_implement"] or "[]"))
        ad = ", ".join(json.loads(f["allowed_deps"] or "[]"))
        fb = ", ".join(json.loads(f["forbidden"] or "[]"))
        out.append(f"- `{f['path']}` ({f['owner_role'] or 'any'}) — {f['purpose']}")
        if mi:
            out.append(f"  - must implement: {mi}")
        if ad:
            out.append(f"  - allowed deps: {ad}")
        if fb:
            out.append(f"  - forbidden: {fb}")
        if f["technical_ac"]:
            out.append(f"  - technical AC: {f['technical_ac']}")
    out += ["", "## Interface contracts"]
    for i in query("SELECT * FROM interface_contracts WHERE project_id = ? "
                   "AND status = 'active' ORDER BY name", (project_id,)):
        out.append(f"- **{i['name']}** ({i['module'] or '—'}): `{i['signature']}`")
    comps = query("SELECT * FROM component_registry WHERE project_id = ? AND status = 'active' "
                  "ORDER BY code", (project_id,))
    if comps:
        out += ["", "## Components"]
        for c in comps:
            out.append(f"- **{c['code']} {c['name']}** (`{c['path'] or '—'}`) — {c['purpose']}")
    return "\n".join(out)


def _sprint_plan_user(project_id: str) -> str:
    files = query("SELECT path, purpose FROM file_contracts WHERE project_id = ? "
                  "AND status = 'active' ORDER BY path", (project_id,))
    contracts = "\n".join(f"- {f['path']}: {f['purpose']}" for f in files[:40])
    adrs = query("SELECT title, decision FROM architecture_decisions WHERE project_id = ? "
                 "AND status = 'accepted' ORDER BY created_at", (project_id,))
    adr_txt = "\n".join(f"- {a['title']}: {a['decision'][:200]}" for a in adrs[:8])
    stories = ""
    d = latest_doc(project_id, "PDS")
    if d:
        stories = "\n\n=== PDS (stories & priorities) ===\n" + d["content_md"][:4000]
    return (_brief_block(project_id) + "\n\n=== Architecture decisions ===\n" + adr_txt
            + "\n\n=== File contracts ===\n" + contracts + stories)


def breakdown_sprints(project_id: str) -> dict:
    """One planning run slices the approved blueprint into ordered Planned
    sprints whose tasks carry the blueprint's technical ACs; every sprint is
    required to leave the app running (prompt rule, checked by sprint gates)."""
    text = _llm(project_id, SPRINT_PLAN_SYSTEM, _sprint_plan_user(project_id),
                max_tokens=6000, label="sprint-plan")
    data = codegen._extract_object(text)
    sprints = data.get("sprints")
    if not isinstance(sprints, list) or not sprints:
        raise SpecsError("Sprint breakdown reply was unusable — say `plan sprints` to retry.")
    # Never silently stack a second plan over planned sprints.
    planned = query("SELECT id FROM sprints WHERE project_id = ? "
                    "AND status IN ('Planned','Ready') ORDER BY created_at", (project_id,))
    if planned:
        execute("UPDATE tasks SET sprint_id = NULL WHERE project_id = ? AND sprint_id IN "
                "(" + ",".join("?" * len(planned)) + ")",
                tuple(p["id"] for p in planned))
        for p in planned:
            execute("DELETE FROM sprints WHERE id = ?", (p["id"],))
    ts = now()
    count = query_one("SELECT COUNT(*) AS n FROM sprints WHERE project_id = ?",
                      (project_id,))["n"]
    contract_ac = {f["path"]: f["technical_ac"] for f in query(
        "SELECT path, technical_ac FROM file_contracts WHERE project_id = ? "
        "AND status = 'active'", (project_id,))}
    sprint_lines, plan_sections, task_total = [], [], 0
    for i, s in enumerate(sprints[:6]):
        if not isinstance(s, dict):
            continue
        sid = new_id()
        name = str(s.get("name") or f"Sprint {count + i + 1}").strip()[:120]
        goal = str(s.get("goal") or "").strip()[:1000]
        tasks = [t for t in (s.get("tasks") or []) if isinstance(t, dict)][:10]
        insert("sprints", {"id": sid, "project_id": project_id, "name": name,
                           "goal": goal, "capacity": 40,
                           "status": "Planned", "created_at": ts})
        pts = 0
        for t in tasks:
            tid = new_id()
            title = str(t.get("title") or t.get("task") or "").strip()[:200]
            if not title:
                continue
            points = max(0, min(13, _int_or(t.get("points"), 3)))
            priority = max(1, min(5, _int_or(t.get("priority"), 2)))
            pts += points
            desc = str(t.get("description") or "")[:2000]
            file_paths = [p for p in (_safe(t.get("files"))) if p][:8]
            if file_paths:
                desc += "\nBlueprint files: " + ", ".join(file_paths)
            tac = "; ".join(ac for p in file_paths
                            if (ac := contract_ac.get(p, "")))[:800]
            insert("tasks", {"id": tid, "project_id": project_id, "sprint_id": sid,
                             "title": title, "description": desc,
                             "acceptance_criteria": str(t.get("acceptance_criteria") or "")[:2000],
                             "technical_ac": tac,
                             "story_points": points, "priority": priority,
                             "status": "Todo", "created_at": ts, "updated_at": ts})
            task_total += 1
        sprint_lines.append(f"**{name}** ({len(tasks)} tasks, {pts} pts) — {goal[:120]}")
        plan_sections.append(f"## {name} — {len(tasks)} tasks, {pts} pts\n"
                             + (goal + "\n" if goal else "")
                             + "\n".join(f"- {t['title']}" for t in tasks
                                         if str(t.get('title') or '').strip()))
    if not sprint_lines:
        raise SpecsError("Sprint breakdown produced no sprints — retry with `plan sprints`.")
    project_name = str(query_one("SELECT name FROM projects WHERE id = ?",
                                 (project_id,))["name"])
    plan_md = ("# Sprint Plan — " + project_name
               + "\n\nEach sprint leaves the app running plus its increment.\n\n"
               + "\n\n".join(plan_sections))
    save_doc(project_id, "SPRINT-PLAN", plan_md, status="approved", actor="solution-architect")
    audit("sprint_breakdown", "project", project_id,
          f"Blueprint sliced into {len(sprint_lines)} planned sprint(s), {task_total} task(s)",
          actor="agent")
    return {"sprints": len(sprint_lines), "tasks": task_total, "sprint_lines": sprint_lines}


def _int_or(value, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _safe(value) -> list:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        rel = codegen._safe_rel_path(str(v or ""))
        if rel:
            out.append(rel)
    return out


# ------------------------------------------------------------------ contract blocks (dev + SA review)


def _task_haystack(task) -> str:
    return " ".join(str(task.get(k) or "") for k in
                    ("title", "description", "acceptance_criteria", "technical_ac")).lower()


def _fmt_contract(f) -> str:
    mi = ", ".join(json.loads(f["must_implement"] or "[]"))
    ad = ", ".join(json.loads(f["allowed_deps"] or "[]"))
    fb = ", ".join(json.loads(f["forbidden"] or "[]"))
    line = f"- `{f['path']}` — {f['purpose']}"
    if mi:
        line += f" | must implement: {mi}"
    if ad:
        line += f" | allowed deps: {ad}"
    if fb:
        line += f" | forbidden: {fb}"
    if f["technical_ac"]:
        line += f" | technical AC: {f['technical_ac']}"
    return line[:400]


def dev_contracts_block(project_id: str, task) -> str:
    """CONTRACTS block for the implementation prompt: the blueprint file
    contracts whose paths/modules the task names, plus matching interface
    contracts. '' when the project has no blueprint or the task matches none."""
    contracts = query("SELECT * FROM file_contracts WHERE project_id = ? AND status = 'active' "
                      "ORDER BY path", (project_id,))
    if not contracts:
        return ""
    hay = _task_haystack(task)
    matched = [c for c in contracts
               if c["path"].lower() in hay or (c["module"] and c["module"].lower() in hay)]
    if not matched:
        return ""
    ifaces = [i for i in query("SELECT * FROM interface_contracts WHERE project_id = ? "
                               "AND status = 'active'", (project_id,))
              if any(m["module"] and (m["module"].lower() in (i["module"] or "").lower()
                                       or m["module"].lower() in (i["name"] or "").lower())
                     for m in matched)]
    lines = ["PROJECT BLUEPRINT CONTRACTS (the Solution Architect owns these — create exactly "
             "these files, expose the listed behaviour, obey the allowed deps and never do the "
             "forbidden things; the SA review gate checks conformance):"]
    lines += [_fmt_contract(c) for c in matched[:10]]
    for i in ifaces[:8]:
        lines.append(f"- interface **{i['name']}**: `{i['signature']}`")
    block = "\n".join(lines)[:_MAX_CONTRACT_BLOCK]
    return "\n" + block + "\n"


def sa_contracts_block(project_id: str, task) -> str:
    """Contract context for the SA review gate — judge the change against
    the WRITTEN blueprint, not a guessed structure."""
    return dev_contracts_block(project_id, task)
