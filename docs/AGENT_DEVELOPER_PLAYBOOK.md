# Agent Developer Playbook

The end-to-end loop for changing what an agent *says* (persona, role
instructions, task wording) and seeing what it *does*, in minutes instead of
an hour. Companion to `docs/AGENT_DEV_SPEEDUP.md`, whose items are now mostly
implemented (deferred items listed at the bottom).

All commands run from the repo root in Git Bash on Windows.

## The fast loop (no live LLM, ~30s per iteration)

```bash
make test-fast            # fatal-error lint + full backend suite (xdist-parallel)
DRY_RUN=1 make run        # start server with stubbed inference (no keys needed)
```

1. **Create or reset demo data** — `make db-reset CONFIRM=yes` wipes the live
   DB (after an automatic backup) and reseeds demo projects/agents. The reseed
   guard means a plain restart never destroys your data.
2. **Open the UI** at http://127.0.0.1:8000 and go to **Playground**.
   Pick your agent → *Preview composition* loads the exact system prompt the
   runtime would send (base stack prompt + role instruction files + persona,
   in override order). The textarea stays editable for one-off experiments.
3. **Tune the prompt.** The real sources live in **Agent Memory**: edit the
   role's instruction files or the persona. Saves return `⚠ lint warnings`
   (size bloat, references to non-existent skills, self-contradicting
   directives) and every content change is snapshotted — use **Version
   history → Restore** to roll back, including to the v1 baseline.
4. **Run it.** The Playground *Run* button sends preview+user prompt through
   the same `call_llm` path as real codegen. Under `DRY_RUN=1` the response is
   a deterministic stub — you are verifying composition, wiring and cost, not
   model output.
5. **Iterate on a whole role from one file** without touching the DB:
   `PERSONA_OVERRIDE=backend/debug/persona.json` replaces role instructions +
   persona for every prompt build (see `codegen._persona_override`).
6. **See the final merged prompt** of real runs:
   `DEBUG_PROMPTS=1` dumps every LLM call (task, workspace, persona, helpers,
   memory all merged) to `backend/debug/prompts/<label>/`.
7. **Then run live**: restart without `DRY_RUN`, execute a task (Templates
   tab in + Add Task give you consistent, complete task wording in 10s), and
   check spend in the cost rollup:
   `GET /api/v1/projects/{id}/costs?by=role|agent|model`.

## Make targets

| Target | What it does |
| --- | --- |
| `make test` | backend (parallel) + frontend unit tests |
| `make test-fast` | lint + backend suite only — the pre-commit loop |
| `make lint` | fatal-error ruff selection (same as CI) |
| `make run` | uvicorn on :8000 (no auto-reload on this box) |
| `make dry-run` | one stubbed LLM call, proves the DRY_RUN path |
| `make frontend` / `make deploy` | build bundle / build + sync into `backend/static` |
| `make db-backup` | timestamped copy of the live DB |
| `make db-reset CONFIRM=yes` | backup, wipe, reseed (destructive, guarded) |

## Environment switches

| Var | Effect |
| --- | --- |
| `DRY_RUN=1` | every LLM call returns a deterministic stub; no API keys needed; full runtime path still executes |
| `DEBUG_PROMPTS=1` | dump final composed prompts to `DEBUG_PROMPTS_DIR` (default `backend/debug/prompts/`) |
| `PERSONA_OVERRIDE=/path.json` | swap persona + role instructions from a local file |
| `FORCE_RESEED=1` | allow the destructive demo reseed on a non-empty DB |
| `AGENT_OFFICE_DB=/path.db` | point at another database (tests use this for isolation) |

## Guardrails already in place

- **Anthropic prompt caching** — the system block is sent with
  `cache_control: ephemeral`, so iterating on a *unchanged* base prompt bills
  cached input on Claude gateways.
- **Per-project budget** — set *LLM budget USD* in project Settings. Task
  starts, auto-eligibility and sprint starts refuse once spend reaches it
  (`TASK_BLOCKED_BY_BUDGET`), *and* every LLM call inside a run re-checks:
  codegen chain fall-through and SA/BA review gates bill against an
  in-flight ledger, so a run stops the moment the cap is hit instead of
  after it finishes. Remaining headroom also shrinks the per-call output
  envelope (8000 → 4000 → 1500 tokens). 0 = unlimited.
- **Golden prompt tests** — `backend/tests/test_golden_prompts.py` pins the
  exact composed system prompts and template renderings. An intentional
  change is a 30-second job: `UPDATE_GOLDEN=1 python -m pytest
  tests/test_golden_prompts.py`, review the diff of `tests/golden/*.txt`.
- **CI** — `.github/workflows/ci.yml` runs lint, parallel pytest, frontend
  tests + build and dependency audits on every push (Python 3.12; the app
  requires 3.11+ for its type syntax). Locally, `pip-audit` / `npm audit`
  are pre-commit *manual-stage* hooks: `pre-commit run --hook-stage manual
  --all-files` (they only fire on commits that touch requirements/lockfiles).

## Windows dev-workflow reminders

- The Makefile picks the interpreter: the app venv at
  `~/.verdent/agentforge-venv` on Windows, `python3` from PATH on
  Linux/macOS. Override anywhere with `make test PY=/path/to/python`.
- uvicorn has **no auto-reload** here: after backend edits, kill the process
  on :8000 and restart; migrations apply automatically at startup.
- After frontend edits: `make deploy` — FastAPI serves `backend/static`, not
  the Vite dev server.

## Deferred from the speed-up guide (with reasons)

- **3.2 A/B prompt canary rollout** — needs two live model/gateway variants
  serving real tasks plus result scoring; overkill for a single-user local
  tool. The Playground + version history covers most of the iteration need.
- **4.5 LLM-as-judge benchmark in CI** — requires real provider keys in CI
  and burns quota on every push; the offline golden-composition tests
  (3.6 variant shipped) catch the regressions this codebase controls.
- **3.6 output-quality goldens** — shipped as *prompt-composition* goldens
  (deterministic, offline). Pinning raw LLM output would be flaky without a
  frozen model version.
