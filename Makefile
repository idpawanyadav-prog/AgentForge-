# AgentForge dev loop (see docs/AGENT_DEVELOPER_PLAYBOOK.md).
# Works from Git Bash on Windows and from Linux CI runners.
# Windows defaults to the app venv; elsewhere use the python on PATH.
# Override anywhere: make test PY=/path/to/python
ifeq ($(OS),Windows_NT)
PY ?= "$$HOME/.verdent/agentforge-venv/Scripts/python.exe"
else
PY ?= python3
endif
NPM ?= npm

.PHONY: test test-fast test-backend test-frontend lint run frontend deploy db-backup db-reset

## Run the whole suite: backend (parallel) + frontend unit tests
test: test-backend test-frontend

## Quick pre-commit loop: fatal lint + parallel backend tests only
test-fast: lint test-backend

## Backend tests, 3-5x faster with pytest-xdist
test-backend:
	cd backend && $(PY) -m pytest tests -n auto -q

test-frontend:
	cd frontend && $(NPM) test

## Fatal-error lint (syntax, undefined names) — same selection as CI
lint:
	cd backend && $(PY) -m ruff check --select E9,F63,F7,F82 app

## Start the backend (no auto-reload on this box; restart after edits)
run:
	cd backend && $(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8000

## Build the frontend bundle
frontend:
	cd frontend && $(NPM) run build

## Sync the built bundle into FastAPI's static dir (required after frontend edits)
deploy: frontend
	rm -rf backend/static/assets && cp -r frontend/dist/. backend/static/

## DRY_RUN smoke: one full no-cost LLM path (returns deterministic stubs)
dry-run:
	cd backend && DRY_RUN=1 $(PY) -c "from app.codegen import call_llm; r = call_llm({'id':'g'},{'provider_model_id':'dry'},'s','u'); print('DRY_RUN ok:', r['dry_run'])"

## Time-stamped copy of the live DB before experiments
db-backup:
	cp backend/agent_office.db backend/agent_office.db.$$(date +%Y%m%d-%H%M%S).bak

## Wipe + reseed the live DB (destructive; requires CONFIRM=yes)
db-reset:
	@test "$(CONFIRM)" = "yes" || { echo "Refusing to wipe the live DB. Re-run with: make db-reset CONFIRM=yes"; exit 1; }
	$(MAKE) db-backup
	rm -f backend/agent_office.db
	cd backend && FORCE_RESEED=1 $(PY) -c "from app import db; db.init_db(); print('fresh DB seeded')"
