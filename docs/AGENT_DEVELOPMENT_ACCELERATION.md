# AgentForge — Agent Development Acceleration Plan

This document outlines concrete steps to speed up agent development,
eliminate the most common failure modes, and make the platform more reliable
for autonomous multi-agent work.

---

## 1. Current Bottlenecks

| Bottleneck | Root cause | Impact |
|---|---|---|
| Slow code generation | Every task generates from scratch; no template reuse | 30-60s per file; LLM quota burn |
| Repeated QA rejections | No institutional memory between tasks | Same mistakes recur across sprints |
| Stale broken tests | Legacy test failures block new tasks | Developers waste time on unrelated failures |
| Merge conflicts | Parallel agents edit overlapping files | Work is lost or requires manual resolution |
| Long self-fix loops | Build/test failures require multiple LLM round-trips | Task duration doubles on failures |
| Sprint planning overhead | Manual creation of sprint, tasks, assignment | 10-15 min per sprint |
| Workspace setup time | Each run copies the full workspace | 2-5s overhead per task |

---

## 2. Acceleration Strategies

### 2.1 Template-based code generation (HIGH IMPACT)

**Problem**: Agents generate code from raw task descriptions. Identical patterns
(CRUD endpoints, test files, config files) are regenerated from scratch each time.

**Solution**:
- Add a `templates/` directory to each project with boilerplate files.
- Before codegen, scan the workspace for existing patterns (routes, models,
  tests) and inject them as context.
- For common patterns (FastAPI CRUD, pytest fixtures), provide scaffold
  templates that the LLM fills in rather than generating from scratch.

**Estimated speedup**: 40-60% reduction in code generation time for common tasks.

**Implementation**:
1. Add `ProjectTemplate` model to the database (per-project or global).
2. In `codegen.py`, before calling the LLM, read matching templates and
   include them in the .
3. Add a "Use template" option in the task creation flow.

### 2.2 Strengthen the pitfall ledger (HIGH IMPACT)

**Problem**: The memory module exists but is underutilized. Agents don't always
read the ledger before generating code, and the deduplication is basic.

**Solution**:
- Make `pitfalls_block()` **mandatory** in every codegen prompt — no exceptions.
- Add severity levels to pitfalls (error, warning, info) and include only
  high-severity ones in the prompt to avoid context bloat.
- Add a `pitfall_resolution` tracking: when a pitfall is no longer relevant
  (the underlying issue is fixed), mark it resolved.
- Surface the top 3 pitfalls in the task detail view so humans can see what
  the agent is being warned about.

**Estimated speedup**: 30-50% reduction in repeat QA rejections.

**Implementation**:
1. In `codegen.py`, add `memory.pitfalls_block(project_id)` to every
   `generate_implementation()` prompt unconditionally.
2. In `memory.py`, add `importance` threshold filtering and `resolved_at`
   timestamp.
3. In the frontend, show pitfalls on the task detail panel.

### 2.3 Pre-flight test quarantine (MEDIUM IMPACT)

**Problem**: Broken legacy test modules block new tasks. The quarantine system
exists but only activates at test time, after the agent has already done its work.

**Solution**:
- Run `pytest --collect-only` **at sprint start**, not at task execution time.
- Quarantine broken modules before any task in the sprint starts.
- Report quarantined modules in the sprint dashboard so humans can fix them
  between sprints.

**Estimated speedup**: Eliminates false QA failures from stale tests.

**Implementation**:
1. In `sprint_gate.py`, add a `preflight_health_check()` call when a sprint
   transitions to `Active`.
2. Store quarantined modules in the sprint record.
3. In the frontend, show quarantine status in the sprint detail view.

### 2.4 Incremental workspace sync (MEDIUM IMPACT)

**Problem**: Every run copies the entire workspace. For large projects, this
takes 2-5 seconds and consumes disk I/O.

**Solution**:
- Use `git worktree` for run isolation instead of full directory copies.
  `git worktree add` creates a lightweight checkout in <1s.
- Alternatively, use overlayfs (Linux) or NTFS hard links (Windows) for
  copy-on-write semantics.

**Estimated speedup**: 70-90% reduction in workspace setup time.

**Implementation**:
1. In `workspace.py`, add a `_worktree_available()` check.
2. If git is available and the workspace is a git repo, use `git worktree add`
   instead of `_copy_for_run()`.
3. Merge-back uses `git diff` + `git apply` instead of file-level copy.

### 2.5 Pre-computed context bundles (MEDIUM IMPIMPACT)

**Problem**: Each agent request re-reads the same context (project description,
sprint goal, team roles, recent events) from the database.

**Solution**:
- Cache the project context bundle in memory (with a TTL).
- Invalidate the cache on project/sprint/team mutations.
- Include the cached bundle in all LLM prompts to avoid repeated DB queries.

**Estimated speedup**: 10-20% reduction in context loading time.

**Implementation**:
1. In `runtime.py`, add a `_context_cache` dict with TTL.
2. In `codegen.py`, accept a pre-built context string instead of reading
   from the database each time.
3. In `chatbot.py`, use the cached context for AI routing.

### 2.6 Parallel task execution with dependency-aware scheduling (HIGH IMPACT)

**Problem**: Tasks are executed one at a time per sprint, even when they have
no dependencies.

**Solution**:
- Run independent tasks in parallel (respecting `MAX_CONCURRENT_RUNS`).
- Use topological sort on the dependency graph to determine execution order.
- When a task fails, only block its dependents — other tasks continue.

**Estimated speedup**: Sprint completion time scales linearly with team size
for independent tasks.

**Implementation**:
1. In `runtime.py`, replace the single-task scheduler with a dependency-aware
   scheduler that claims multiple ready tasks per tick.
2. In `task_registry.py`, add a dependency graph validator.
3. In the frontend, show parallel task execution lanes in the task board.

### 2.7 Warm model connections (LOW-MEDIUM IMPACT)

**Problem**: Each LLM call creates a new HTTP connection. Under burst loads
(multiple agents generating code simultaneously), this causes connection
exhaustion and slow first-call latency.

**Solution**:
- Maintain a pool of warm `httpx.AsyncClient` connections per gateway.
- Pre-warm connections when a gateway is marked healthy.
- Reuse connections across codegen calls within the same task.

**Estimated speedup**: 15-30% reduction in LLM call latency.

**Implementation**:
1. In `codegen.py`, add a connection pool per gateway.
2. In `llm/health.py`, pre-warm connections after a successful health check.
3. Add connection pool metrics to the gateway health response.

### 2.8 Faster dev self-test feedback (MEDIUM IMPACT)

**Problem**: The self-fix loop re-runs the full build + test suite on each
attempt. For a FastAPI project, this takes 30-60s per attempt.

**Solution**:
- On the first self-fix attempt, run only the **failing** test (or the build
  check if there was no test failure).
- Only run the full suite on the final attempt.
- Cache build artifacts (`.pyc`, `node_modules`, `.venv`) between attempts.

**Estimated speedup**: 60-80% reduction in self-fix loop time.

**Implementation**:
1. In `runtime.py`, parse the test summary to identify the specific failing
   test and run only that test on retry.
2. In `toolchains.py`, add artifact caching between invocations within a run.
3. In `workspace.py`, keep the run workspace alive between self-fix attempts
   (it's already isolated).

### 2.9 Sprint auto-planning from backlog (MEDIUM IMPACT)

**Problem**: Creating a sprint requires manually selecting backlog items,
estimating capacity, and assigning agents.

**Solution**:
- Add a "Auto-plan sprint" command that:
  1. Reads the top-priority backlog items.
  2. Estimates capacity based on agent availability and historical velocity.
  3. Creates tasks with acceptance criteria derived from backlog item descriptions.
  4. Assigns tasks to the best-matching agent (by role, current load, and
     past performance on similar tasks).
- The chatbot already has `create_sprint` — extend it with auto-planning.

**Estimated speedup**: Sprint planning from 10-15 min to <30s.

**Implementation**:
1. In `services/tasks.py`, add `auto_plan_sprint(project_id, sprint_id)`.
2. In `chatbot.py`, add `auto_plan` intent that calls the service.
3. In the frontend, add an "Auto-plan" button in the sprint creation flow.

### 2.10 Agent specialization and caching (LOW IMPACT, long-term)

**Problem**: All agents use the same base model and prompts. There is no
  specialization for specific task types (frontend, backend, database, tests).

**Solution**:
- Add task-type-specific personas that are automatically selected based on
  the task's tags/category.
- Cache successful implementations for common patterns and reuse them
  (with adaptation) for similar tasks.
- Track per-agent success rates by task type and prefer high-performing
  agents for matching tasks.

**Estimated speedup**: 10-20% improvement in first-attempt success rate.

**Implementation**:
1. In `roles.py`, add a `task_types` field to personas.
2. In `services/tasks.py`, add task type classification.
3. In `runtime.py`, select the persona based on task type.

---

## 3. Quick Wins (implement in <1 day each)

| # | Action | File(s) | Impact |
|---|---|---|---|
| 1 | Make `pitfalls_block()` mandatory in all codegen prompts | `codegen.py` | High |
| 2 | Add `_summary_cache` locking | `routers/tasks.py` | Medium |
| 3 | Run quarantine at sprint start, not task time | `sprint_gate.py`, `toolchains.py` | Medium |
| 4 | Add `partialize` to Zustand persist | `frontend/src/store.ts` | Medium |
| 5 | Add SSE connection limit per IP | `routers/events.py` | Low |
| 6 | Call `validate_workspace_path()` on project creation | `routers/projects.py` | Low |
| 7 | Add `/health` endpoint | `main.py` | Low |
| 8 | Add correlation IDs to logs | `main.py`, `db.py` | Low |

---

## 4. Medium-Term Improvements (1-2 weeks each)

| # | Action | Files | Impact |
|---|---|---|---|
| 1 | Template-based code generation | `codegen.py`, new `templates/` | High |
| 2 | Parallel task execution | `runtime.py`, `task_registry.py` | High |
| 3 | Git worktree isolation | `workspace.py` | Medium |
| 4 | Context bundle caching | `runtime.py`, `codegen.py` | Medium |
| 5 | Self-fix scoped testing | `runtime.py`, `toolchains.py` | Medium |
| 6 | Sprint auto-planning | `services/tasks.py`, `chatbot.py` | Medium |
| 7 | Warm connection pools | `codegen.py`, `llm/health.py` | Low-Medium |
| 8 | Standardized error envelope | All routers | Low |

---

## 5. Long-Term Improvements (1+ month each)

| # | Action | Impact |
|---|---|---|
| 1 | Agent specialization and caching | Medium |
| 2 | CI/CD pipeline | High (reliability) |
| 3 | Metrics export (Prometheus) | Medium (observability) |
| 4 | ADRs and architecture docs | Low (maintainability) |
| 5 | Frontend test expansion | Medium (quality) |
| 6 | Key rotation for encrypted secrets | Medium (security) |

---

## 6. Recommended Implementation Order

```
Week 1: Quick wins (items 1-8 in section 3)
Week 2-3: Template-based code generation (2.1)
Week 3-4: Strengthen pitfall ledger (2.2)
Week 4-5: Pre-flight test quarantine (2.3)
Week 5-6: Parallel task execution (2.6)
Week 6-7: Faster self-fix feedback (2.8)
Week 7-8: Sprint auto-planning (2.9)
Ongoing: CI/CD, monitoring, documentation
```

---

## 7. Success Metrics

Track these metrics before and after each acceleration to measure impact:

| Metric | How to measure |
|---|---|
| Average task completion time | `workflow_runs.started_at` → `completed_at` |
| First-attempt success rate | % of tasks reaching QA without rework |
| QA rejection rate | % of QA runs that reject |
| Repeat mistake rate | % of rejections matching an existing pitfall |
| Workspace setup time | Time from `start_execution` to first agent activity |
| LLM token consumption per task | `usage_records.input_tokens` + `output_tokens` |
| Sprint completion rate | % of sprints where all tasks reach Done |
| Agent idle time | % of time agents spend in "Idle" state |
