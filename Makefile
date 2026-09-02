# DJ mixer — test and local-environment orchestration.
#
#   make help          list every target
#   make test          full suite on SQLite (the default backend)
#   make test-pg       full suite against a local PostgreSQL instance
#   make check         both of the above, plus the browser suite
#
# ---------------------------------------------------------------------------
# On teardown, and why the targets are split the way they are
# ---------------------------------------------------------------------------
# Ingested audio is the expensive artifact here, not the database. Masters
# under $(DATA_DIR)/audio came from Jamendo, whose free tier is metered
# monthly, so deleting them means re-spending quota that does not come back
# until the month rolls over. Rendered variants are cheap in quota but cost
# ~60s of CPU each to re-render.
#
# So teardown is tiered, and nothing destructive runs as a side effect of a
# test target:
#
#   make down          stop Postgres. Touches no data. Safe, reversible.
#   make clean-pg      delete the Postgres cluster. Safe: the catalog is
#                      rebuilt from local masters with ZERO API requests.
#   make clean-variants  delete rendered variants. Costs CPU, not quota.
#   make clean-audio   DELETES INGESTED MASTERS. Costs Jamendo quota to undo.
#   make clean-all     everything above.
#
# The last two refuse to run without CONFIRM=yes, and print what will be lost
# first. `make clean-pg` is deliberately NOT guarded, because the publisher
# reuses masters and their metadata sidecars — see backend/publish.py.

SHELL := /bin/bash
.DEFAULT_GOAL := help

ROOT     := $(shell cd "$(dir $(lastword $(MAKEFILE_LIST)))" && pwd)
DATA_DIR ?= $(ROOT)/data
VENV     ?= $(ROOT)/.venv
PY       := $(shell \
              if [ -x "$(VENV)/bin/python" ]; then echo "$(VENV)/bin/python"; \
              else common=$$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null); \
                   cand="$$(dirname "$$common")/.venv/bin/python"; \
                   if [ -x "$$cand" ]; then echo "$$cand"; else echo python3; fi; \
              fi)

# -- local PostgreSQL -------------------------------------------------------
# A local cluster rather than a container: postgres is already on PATH via
# homebrew, and this needs no daemon, no image pull and no root. The whole
# instance is one directory, so `clean-pg` is an rm -rf.
#
# It lives OUTSIDE the repository, keyed by repo path so parallel worktrees get
# their own. An in-tree cluster is ~50 MB of churning binary files that a
# `git add -A` during a conflict resolution will happily commit — which is not
# hypothetical, it happened. .gitignore is not sufficient protection, because a
# rebase can replay a commit whose .gitignore does not yet list it. Keeping the
# cluster out of the tree removes the failure mode rather than guarding it.
PGDATA   ?= $(HOME)/.cache/djmixer-pg/$(shell echo "$(ROOT)" | shasum | cut -c1-12)
PGPORT   ?= 5433
PGDB     ?= djmixer
# The suite gets its OWN database, recreated per run. The SQLite fixture builds
# a fresh temp file every time; Postgres would otherwise persist between runs,
# and main's ingestion is resumable — it sees stale tracks at `ready`, skips
# them, and the suite then asserts against a catalog it never built. Keeping
# the test database separate from $(PGDB) also means dropping it is obviously
# safe and needs no confirmation: nothing but the suite ever writes to it.
PGTESTDB ?= djmixer_test
PGUSER   := $(shell id -un)
PG_URL   := postgresql://$(PGUSER)@127.0.0.1:$(PGPORT)/$(PGDB)
PG_TEST_URL := postgresql://$(PGUSER)@127.0.0.1:$(PGPORT)/$(PGTESTDB)
PGLOG    := $(PGDATA)/server.log

PORT     ?= 5050

.PHONY: help test test-fast test-pg test-smoke test-browser check serve ingest ingest-dry env \
        db-up db-down db-reset db-shell db-url wait-pg up down status \
        clean-pg clean-variants clean-audio clean-all deps

help:
	@echo "Testing"
	@echo "  make test            backend + frontend suites (SQLite)"
	@echo "  make test-fast       same, skipping the browser suite"
	@echo "  make test-pg         DB-layer suite against local PostgreSQL"
	@echo "  make test-smoke      boot the service against PostgreSQL (synthetic catalog)"
	@echo "  make test-browser    Playwright suite only"
	@echo "  make check           everything: SQLite, PostgreSQL, browser"
	@echo
	@echo "Environment"
	@echo "  make up              start PostgreSQL (creates the cluster if needed)"
	@echo "  make down            stop PostgreSQL. No data is deleted."
	@echo "  make status          what is running, and what is on disk"
	@echo "  make serve           run the app on :$(PORT)"
	@echo "  make db-shell        psql into the local database"
	@echo "  make env             show the resolved configuration"
	@echo "  make db-reset        drop + recreate the suite's database (safe)"
	@echo
	@echo "Catalog"
	@echo "  make ingest-dry      show the plan and API cost. Spends nothing."
	@echo "  make ingest          batch-publish the catalog (parallel, resumable)"
	@echo
	@echo "Teardown (tiered by what it costs to undo)"
	@echo "  make clean-pg        drop the PostgreSQL cluster    [free to rebuild]"
	@echo "  make clean-variants  drop rendered variants         [costs CPU]"
	@echo "  make clean-audio     drop ingested masters          [COSTS JAMENDO QUOTA]"
	@echo "  make clean-all       all of the above               [COSTS JAMENDO QUOTA]"
	@echo "  The last two require CONFIRM=yes."

deps:
	@test -x "$(PY)" || command -v "$(PY)" >/dev/null || { \
	  echo "No usable Python found. Create one:" >&2; \
	  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements-ingest.txt" >&2; \
	  exit 1; }
	@"$(PY)" -c "import numpy, flask" 2>/dev/null || { \
	  echo "$(PY) lacks numpy/flask. Install requirements-ingest.txt into it." >&2; \
	  exit 1; }
	@echo "python: $$("$(PY)" -c 'import sys; print(sys.executable, sys.version.split()[0])')"

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

test: deps
	@./run_tests.sh

test-fast: deps
	@./run_tests.sh --fast

test-browser: deps
	@test -d node_modules || { \
	  echo "node_modules missing. In a worktree, symlink the main checkout's:" >&2; \
	  echo "  ln -s <main-checkout>/node_modules node_modules" >&2; \
	  echo "Then remove it before committing — .gitignore does not match symlinks." >&2; \
	  exit 1; }
	@npx playwright test

# The suite runs identically against PostgreSQL; only DJMIXER_DATABASE_URL
# changes. This is the check that the dialect layer actually works rather than
# merely emitting plausible SQL — nothing else exercises a live server.
# Scoped to the P5 modules, deliberately, and NOT because the rest fails on
# Postgres. The rest of the suite depends on per-module database isolation that
# only SQLite provides: each module points config.DB_PATH at its own temp file
# and gets a private database for free, while Database.from_config() under
# Postgres ignores DB_PATH and hands every module the same DJMIXER_DATABASE_URL.
# test_p6_resumable then writes an 8-second track 1001 over the shared
# fixture's 60-second one, and the P3/P4 assertions fail against a catalog they
# did not build. That is cross-module contamination inside a single run, so
# recreating the database beforehand cannot fix it — the fixtures would need
# per-module schemas.
#
# The P5 modules are the dialect-relevant ones (schema, engine, admission
# control, storage seam) and they build their catalogs through the shared
# fixture, so they are safe to share a database.
PG_TEST_MODULES ?= test_p5_*.py

test-pg: deps db-up db-reset
	@echo "== Backend suite against PostgreSQL ($(PG_TEST_MODULES)) =="
	@DJMIXER_DATABASE_URL="$(PG_TEST_URL)" "$(PY)" -m unittest discover \
	  -s tests/backend -t tests/backend -p "$(PG_TEST_MODULES)"

# Drop and recreate the suite's database. Derived data only — no Jamendo
# requests are needed to rebuild it, so this runs without confirmation.
db-reset: db-up
	@dropdb -h 127.0.0.1 -p $(PGPORT) -U "$(PGUSER)" --if-exists "$(PGTESTDB)"
	@createdb -h 127.0.0.1 -p $(PGPORT) -U "$(PGUSER)" "$(PGTESTDB)"

# The service smoke test: boots the real app against PostgreSQL with a
# synthetic catalog — schema, ingestion, warmup, endpoints — which is what CI
# runs and what nothing else covers end to end. Its own database, because it
# drops the schema to guarantee a genuine first boot.
PGSMOKEDB ?= djmixer_smoke
PG_SMOKE_URL := postgresql://$(PGUSER)@127.0.0.1:$(PGPORT)/$(PGSMOKEDB)

test-smoke: deps db-up
	@dropdb -h 127.0.0.1 -p $(PGPORT) -U "$(PGUSER)" --if-exists "$(PGSMOKEDB)"
	@createdb -h 127.0.0.1 -p $(PGPORT) -U "$(PGUSER)" "$(PGSMOKEDB)"
	@echo "== Service smoke test (PostgreSQL + synthetic catalog) =="
	@DJMIXER_SMOKE_DATABASE_URL="$(PG_SMOKE_URL)" "$(PY)" -m unittest discover \
	  -s tests/backend -t tests/backend -p "test_p7_*.py"

_test-backend:
	@cd tests/backend && "$(PY)" -m unittest discover -s .

check: test-fast test-pg test-smoke test-browser
	@echo
	@echo "All suites passed (SQLite, PostgreSQL, service smoke, browser)."

# ---------------------------------------------------------------------------
# Local PostgreSQL
# ---------------------------------------------------------------------------

$(PGDATA)/PG_VERSION:
	@command -v initdb >/dev/null || { \
	  echo "initdb not found. Install PostgreSQL (brew install postgresql@16)." >&2; \
	  exit 1; }
	@echo "Creating PostgreSQL cluster at $(PGDATA)"
	@mkdir -p "$(dir $(PGDATA))"
	@initdb -D "$(PGDATA)" -U "$(PGUSER)" --auth=trust >/dev/null

db-up up: $(PGDATA)/PG_VERSION
	@if pg_isready -h 127.0.0.1 -p $(PGPORT) -q 2>/dev/null; then \
	  echo "PostgreSQL already up on :$(PGPORT)"; \
	else \
	  echo "Starting PostgreSQL on :$(PGPORT)"; \
	  pg_ctl -D "$(PGDATA)" -l "$(PGLOG)" \
	    -o "-p $(PGPORT) -k $(PGDATA) -c listen_addresses=127.0.0.1" -w start >/dev/null; \
	fi
	@$(MAKE) --no-print-directory wait-pg
	@psql -h 127.0.0.1 -p $(PGPORT) -U "$(PGUSER)" -d postgres -tAc \
	   "SELECT 1 FROM pg_database WHERE datname='$(PGDB)'" | grep -q 1 \
	  || createdb -h 127.0.0.1 -p $(PGPORT) -U "$(PGUSER)" "$(PGDB)"
	@echo "ready: $(PG_URL)"

wait-pg:
	@for i in $$(seq 1 30); do \
	  pg_isready -h 127.0.0.1 -p $(PGPORT) -q 2>/dev/null && exit 0; \
	  sleep 0.5; \
	done; \
	echo "PostgreSQL did not become ready; see $(PGLOG)" >&2; exit 1

# Stops the server. Deletes nothing — the cluster and all catalog data survive.
db-down down:
	@if pg_isready -h 127.0.0.1 -p $(PGPORT) -q 2>/dev/null; then \
	  pg_ctl -D "$(PGDATA)" -m fast -w stop >/dev/null && echo "PostgreSQL stopped."; \
	else \
	  echo "PostgreSQL is not running."; \
	fi
	@echo "No data was deleted. 'make clean-pg' drops the cluster."

db-shell: db-up
	@psql "$(PG_URL)"

db-url:
	@echo "$(PG_URL)"

# What the app will actually use, after .env > .env.local > defaults resolve.
# Worth a target because the failure it prevents is silent: an unset
# DJMIXER_DATABASE_URL falls back to SQLite and everything still works, right
# up until the dialect differences surface in production.
env:
	@"$(PY)" -c "from backend import config, storage; \
	  print('database   :', config.database_url()); \
	  print('blob store :', type(storage.get_store()).__name__); \
	  print('data dir   :', config.DATA_DIR); \
	  print('catalog    :', config.TRACKS_CONFIG); \
	  print('jamendo id :', 'set' if config.jamendo_client_id() else 'NOT SET (offline mode only)')"

status:
	@echo "python:      $(PY)"
	@printf "postgres:    "
	@pg_isready -h 127.0.0.1 -p $(PGPORT) 2>/dev/null || echo "not running (:$(PGPORT))"
	@printf "cluster:     "; [ -d "$(PGDATA)" ] && du -sh "$(PGDATA)" 2>/dev/null | cut -f1 || echo "absent"
	@printf "masters:     "; [ -d "$(DATA_DIR)/audio" ] \
	  && echo "$$(ls -1 "$(DATA_DIR)/audio" 2>/dev/null | wc -l | tr -d ' ') file(s), $$(du -sh "$(DATA_DIR)/audio" 2>/dev/null | cut -f1)" \
	  || echo "none"
	@printf "variants:    "; [ -d "$(DATA_DIR)/variants" ] \
	  && echo "$$(ls -1 "$(DATA_DIR)/variants" 2>/dev/null | wc -l | tr -d ' ') file(s), $$(du -sh "$(DATA_DIR)/variants" 2>/dev/null | cut -f1)" \
	  || echo "none"
	@printf "sqlite:      "; [ -f "$(DATA_DIR)/catalog.sqlite3" ] \
	  && du -sh "$(DATA_DIR)/catalog.sqlite3" | cut -f1 || echo "absent"

# ---------------------------------------------------------------------------
# App and catalog
# ---------------------------------------------------------------------------

serve: deps
	@"$(PY)" -c "from backend.app import create_app; \
	  create_app().run(host='127.0.0.1', port=$(PORT))"

ingest-dry: deps
	@"$(PY)" -m backend.publish --dry-run

ingest: deps
	@"$(PY)" -m backend.publish $(PUBLISH_ARGS)

# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
# Guarded targets print what will be lost, then refuse without CONFIRM=yes.
# They are never invoked by any test target.

# Unguarded on purpose: the catalog is derived data. backend/publish.py reuses
# local masters and their metadata sidecars, so rebuilding after this costs
# zero API requests.
clean-pg:
	@$(MAKE) --no-print-directory db-down
	@rm -rf "$(PGDATA)"
	@echo "Dropped the PostgreSQL cluster. Rebuild with 'make up && make ingest'"
	@echo "(no Jamendo requests: masters and metadata are reused from disk)."

clean-variants:
	@n=$$(ls -1 "$(DATA_DIR)/variants" 2>/dev/null | wc -l | tr -d ' '); \
	if [ "$$n" = "0" ]; then echo "No variants to remove."; exit 0; fi; \
	if [ "$(CONFIRM)" != "yes" ]; then \
	  echo "Would delete $$n rendered variant(s) from $(DATA_DIR)/variants."; \
	  echo "Re-rendering costs CPU (~60s/track) but NO Jamendo quota."; \
	  echo "Re-run with: make clean-variants CONFIRM=yes"; exit 1; \
	fi; \
	rm -rf "$(DATA_DIR)/variants"; echo "Removed $$n variant(s)."

clean-audio:
	@n=$$(ls -1 "$(DATA_DIR)/audio" 2>/dev/null | wc -l | tr -d ' '); \
	if [ "$$n" = "0" ]; then echo "No masters to remove."; exit 0; fi; \
	if [ "$(CONFIRM)" != "yes" ]; then \
	  echo "REFUSING: this would delete $$n ingested master(s)."; \
	  echo; \
	  echo "  $(DATA_DIR)/audio  ($$(du -sh "$(DATA_DIR)/audio" 2>/dev/null | cut -f1))"; \
	  echo; \
	  echo "These came from Jamendo. Re-ingesting them spends monthly API"; \
	  echo "quota that does not reset until the month rolls over, and the API"; \
	  echo "publishes no way to read remaining quota."; \
	  echo; \
	  echo "Nothing else in this Makefile requires deleting them: the"; \
	  echo "database can be rebuilt from these files for free."; \
	  echo; \
	  echo "If you are certain: make clean-audio CONFIRM=yes"; exit 1; \
	fi; \
	rm -rf "$(DATA_DIR)/audio" "$(DATA_DIR)/meta"; \
	echo "Removed $$n master(s). Re-ingesting will re-download from Jamendo."

clean-all:
	@if [ "$(CONFIRM)" != "yes" ]; then \
	  echo "REFUSING: 'clean-all' includes ingested masters (Jamendo quota)."; \
	  echo "Run 'make status' to see what exists, or delete a single tier:"; \
	  echo "  make clean-pg                     free"; \
	  echo "  make clean-variants CONFIRM=yes   costs CPU"; \
	  echo "  make clean-audio    CONFIRM=yes   costs quota"; \
	  echo; \
	  echo "If you are certain: make clean-all CONFIRM=yes"; exit 1; \
	fi; \
	$(MAKE) --no-print-directory clean-pg; \
	rm -rf "$(DATA_DIR)" "$(ROOT)/data-e2e" "$(ROOT)/test-results"; \
	echo "Removed the cluster, the catalog, and all ingested audio."
