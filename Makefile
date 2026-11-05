.PHONY: install migrate migration-check unit api integration serve seed

serve:
	.venv/bin/python -m uvicorn pymicroservices.app.main:create_app --factory --host 127.0.0.1 --port 8000 --reload --reload-dir pymicroservices

install:
	python3 -m venv .venv
	.venv/bin/python -m pip install -r pymicroservices/requirements-dev.lock

migrate:
	.venv/bin/python -m pymicroservices.app.migrate upgrade

migration-check:
	.venv/bin/python -m pymicroservices.app.migrate check

# Demo firm with every role (dev sign-in). Needs AUTH_PROVIDER=dev and ADMIN_REQUIRE_MFA=false.
seed:
	.venv/bin/python -m scripts.seed_dev

unit:
	.venv/bin/python -m pytest tests/unit -q

# Sign-in, roles, walls and collection uploads, on Postgres (own *_test schema).
api:
	.venv/bin/python -m pytest tests/api -q

integration:
	RAG_RUN_INTEGRATION=1 .venv/bin/python -m pytest tests/integration -q

# Explicit download only; obtain user approval before running.
reranker-model:
	.venv/bin/python -m pymicroservices.app.utilities.retrieval_models
