# AgentForge — Agent Development Speed-Up Guide

## Executive Summary

The current development loop for an AI agent involves: (1) implementing a
feature in a role, (2) assigning a task that exercises the role, (3) running
the workflow, (4) reviewing the output, (5) fixing issues. Each cycle can
take 5-15 minutes for a real LLM call, and much more if there are errors.

The following strategies reduce that cycle time and eliminate common failure
modes.

---

## Phase 1: Immediate Wins (no-code changes, 1-2 days)

### 1.1 Add `pip-audit` and `npm audit` to pre-commit hooks

**Cost**: 1 hour. **Benefit**: Prevents accidental dependency updates
breaking the Python 3.9 test environment.

```yaml
# .pre-commit-config.yaml
- repo: https://github.com/pyupio/safety
  rev: 2.3.5
  hooks:
    - id: safety
```

Also add `ruff` lint + type checking via `mypy` as pre-commit hooks.

---

### 1.2 Add a single GitHub Actions CI workflow

**Cost**: 2 hours. **Benefit**: Every push is verified. No more "it
worked on my machine" PRs.

Minimal CI (`.github/workflows/ci.yml`):
```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r backend/requirements.txt
      - run: pytest backend/tests/
      - uses: actions/setup-node@v4
        with: { node-version: "20" }
      - run: cd frontend && npm ci && npm test && npm run build
```

---

### 1.3 Add `pytest-xdist` for parallel test execution

**Cost**: 30 min. **Benefit**: Test suite runs 3-5x faster.

```bash
pip install pytest-xdist
pytest backend/tests/ -n auto
```

---

### 1.4 Add a Makefile or Taskfile for common operations

**Cost**: 1 hour. **Benefit**: New developers and agent developers can
run "the standard dev loop" without reading README.

```makefile
# Makefile
test:          ## Run all tests (backend + frontend)
	@cd backend && pytest tests/
	@cd frontend && npm test

db-reset:      ## Reset database to seed state
	@cd backend && python -m app.migrations.reset

run:           ## Start backend with auto-reload
	@cd backend && uvicorn app.main:app --reload

frontend:     ## Build frontend
	@cd frontend && npm run build
```

---

## Phase 2: Agent Developer Experience (2-3 days)

### 2.1 Add a task template system

**Problem**: Each new task has to be hand-crafted with title, description,
acceptance criteria, and evidence. Many tasks repeat patterns.

**Solution**: Create task templates in `backend/app/templates/`:

```
templates/
  api-endpoint.json     # FastAPI router + CRUD
  auth-middleware.json  # JWT auth middleware
  database-migration.json # Alembic migration
  unit-test.json        # pytest test for a function
```

Each template defines: title prefix, description template, acceptance
criteria, related files. The PO agent fills the template at creation time,
and the QA agent knows exactly what the task should contain.

**Benefit**: Consistent task quality, faster task creation, predictable
agent behavior.

---

### 2.2 Add a "dry run" mode for LLM calls

**Problem**: Testing role instruction changes requires burning real LLM
tokens. Iterating on a persona is expensive.

**Solution**: Add a `DRY_RUN=true` env var. When set, `call_llm()` returns a
deterministic stub response instead of hitting the provider:

```python
if os.environ.get("DRY_RUN"):
    return {
        "text": '{"summary": "dry run", "files": []}',
        "input_tokens": 0, "output_tokens": 0,
    }
```

**Benefit**: Test prompt changes in milliseconds with zero cost.

---

### 2.3 Add prompt tracing/versioning

**Problem**: It's hard to debug why an agent produced bad output. The  is computed from many sources (task, workspace, memory, persona, helpers)
and you can't see the final prompt.

**Solution**: Store the final  (and user message) in a
`prompt_log` table or to a `debug/prompts/` directory when `DEBUG_PROMPTS=1`:

```
debug/prompts/
  task-abc/
    dev-phase1-impl-2024-01-15T10:30:00Z.txt
    qa-phase2-review-2024-01-15T10:32:00Z.txt
```

Include: timestamp, agent, role, mode, model, full  + user prompt.

**Benefit**: Debug bad output in seconds. Identify which source file
caused the agent to misbehave.

---

### 2.4 Add a "reset to baseline" button per role

**Problem**: Users edit persona instructions, then can't remember the
original. They keep iterating and never reach the baseline.

**Solution**: The original seeded content is stored as `role_instruction_baselines`.
Each instruction file row has a `baseline_id` pointing to its original. The
UI shows "Reset to default" which copies the baseline back.

**Benefit**: Users can freely experiment and recover with one click.

---

### 2.5 Add an end-to-end "playbook" for new agents

**File**: `docs/AGENT_DEVELOPER_PLAYBOOK.md`

Structure:
1. Run `make db-reset && make run`
2. Create a test project in the UI
3. Create a test sprint
4. Create a test task (use the template)
5. Run it with a real model (or DRY_RUN)
6. View the output workspace
7. Adjust the persona/role instructions
8. Re-run and compare outputs

**Benefit**: New contributors know exactly how to verify their changes.

---

## Phase 3: Platform Hardening (1-2 weeks)

### 3.1 Add prompt caching

**Benefit**: Reduce token costs by 30-50% for repeated prompts.

For Anthropic, use `"cache_control": {"type": "ephemeral"}` on the 
portion. For OpenAI, use the `caching` header.

The caching key is the  content. The  doesn't
change within a task run, so the second phase call in a task would hit cache.

---

### 3.2 Add prompt A/B testing framework

**Problem**: When changing a persona's instructions, you don't know if it
improves or worsens agent behavior.

**Solution**: Each persona change can be deployed as a variant. The
platform runs tasks against both variants (canary 5% → 25% → 100%) and
measures: acceptance rate, rework cycles, user satisfaction. Roll back if
the variant regresses.

```python
# In codegen.py
variant = get_persona_variant(agent_id, mode)
if variant:
    system_prompt = variant.prompt  # Instead of default
```

---

### 3.3 Add token budget enforcement

**Problem**: A single task can exceed $10-50 in LLM costs if it burns
through retries.

**Solution**: 
- Compute a max budget per task based on project settings
- On each LLM call, check remaining budget
- Abort with a clear message if over budget

```python
budget = get_project_budget(project_id)  # in dollars
cost_so_far = get_cost_so_far(run_id)
if cost_so_far + estimated_next_call_cost > budget:
    return error("Task exceeded budget. Projected cost: $X")
```

---

### 3.4 Implement workspace isolation per task run

**Problem**: Concurrent tasks in the same project overwrite each other's
writes.

**Solution**: Each run gets an isolated workspace:
```
workspace_path/.runs/{run_id}/
  app/
  tests/
```

On completion, the changes are diffed and applied atomically to the
shared workspace. If the task fails, the isolated workspace is deleted.

**Benefit**: True parallelism, no race conditions, easier debugging
(compare before/after by folder).

---

### 3.5 Add per-role model cost tracking

**Problem**: Agents are assigned model bindings but there's no way to see
how much each role costs across sprints.

**Solution**: Add a `role_cost_breakdown` API endpoint:

```
GET /api/v1/projects/{id}/costs?by=role&sprint=3
  → {
    "Senior Developer": { "calls": 47, "input_tokens": 1.2M, "cost": $12.40 },
    "QA Engineer": { "calls": 22, "input_tokens": 0.8M, "cost": $5.80 },
    ...
  }
```

**Benefit**: Optimize model assignments by role (use cheaper models for
simple roles).

---

### 3.6 Add regression test against prompt quality

**Problem**: When a developer modifies a persona or instruction file, they
can't tell if they made the agent better or worse.

**Solution**: Maintain a small "golden test set" of tasks with known-good
outputs (reference files, expected summary). When any instruction or
persona changes, run the test set against a live model and diff the output.
If output regresses, flag it.

```
backend/tests/golden/
  sr-dev-task-api-endpoint/
    expected_files/  # reference file content
    expected_summary.md
  ba-task-gather-requirements/
    expected_files/
    expected_summary.md
```

---

## Phase 4: Developer Velocity (continuous)

### 4.1 Use a separate branch for each role persona change

When editing persona instructions for Role X, the developer should be able
to test Role X's behavior without affecting the other roles.

**Current workaround**: Use two projects with different personas.
**Recommended**: Add a `--persona-override` flag to the test runner that
loads a persona from a local JSON file instead of the database.

---

### 4.2 Add a "role playground" in the UI

A simple form where a developer can:
1. Select a role (e.g., Senior Developer)
2. Write a task description
3. See the role's persona, instruction files, and the full 
4. Run the task and see the output files

This lets developers iterate on instructions in seconds without creating a
full project/sprint/task.

---

### 4.3 Pre-seed instruction files with version history

Currently `instruction_files` has a `version` column that's incremented on
edit but old versions aren't retained. Add `instruction_file_versions`:

```sql
CREATE TABLE instruction_file_versions (
  id TEXT PRIMARY KEY,
  instruction_file_id TEXT REFERENCES instruction_files(id),
  version INTEGER,
  content TEXT,
  checksum TEXT,
  created_at TEXT
);
```

**Benefit**: Compare role instruction changes over time. Roll back to a
specific version.

---

### 4.4 Use a prompt linter

**Problem**: Persona instructions are free-form text. Some can be vague,
ambiguous, or contradict the .

**Solution**: Add a lightweight linter that runs on save:
- Detects persona instructions that reference non-existent skills
- Flags instructions that are too long (>2000 tokens) — warn about cost
- Detects contradictory instructions (e.g., one line says "approve fast"
  and another says "be thorough")

---

### 4.5 Benchmark runner for prompt quality

Create a tiny benchmark suite that runs the same 5 tasks with each role's
persona and measures: output quality (via LLM-as-judge), token count,
rework cycles. Run it as part of CI when any instruction file changes.

This makes persona development a measurable activity.

---

## Recommended Development Loop

Here's the improved loop after implementing these suggestions:

```
1. Create a test task (5 min) — or use DRY_RUN + golden set (30s)
2. Write/modify a persona instruction (2 min)
3. Run the task with DRY_RUN or golden set (30s)
4. Review diff of output vs. golden (30s)
5. If output improved, commit and optionally run live
   If not, revert (1 min)
```

vs. current:
```
1. Create a test project, sprint, task (15 min)
2. Write/modify a persona instruction (2 min)
3. Run live task (5-10 min)
4. Review output (2 min)
5. Fix issues, iterate (5-10 min)
```

---

## Quick Wins Checklist

- [ ] Add `DRY_RUN=true` env var (30 min)
- [ ] Add `DEBUG_PROMPTS=1` prompt dump (30 min)
- [ ] Add `make test` target (30 min)
- [ ] Add GitHub Actions CI (2 hours)
- [ ] Add `pytest-xdist` for parallel tests (30 min)
- [ ] Rename `test_*_fixes.py` to behavior-focused names (1 hour)
- [ ] Add `pip-audit` + `npm audit` to CI (1 hour)
- [ ] Add prompt tracing for debugging (2 hours)
- [ ] Add golden test set (4 hours)
- [ ] Add role playground UI page (6 hours)

**Total**: ~15 hours of work for a dramatically improved developer experience.
