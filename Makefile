.PHONY: install migrate migration-check unit integration serve

serve:
	.venv/bin/python -m uvicorn pymicroservices.app.main:create_app --factory --host 127.0.0.1 --port 8000

install:
	python3 -m venv .venv
	.venv/bin/python -m pip install -r pymicroservices/requirements-dev.lock

migrate:
	.venv/bin/python -m pymicroservices.app.migrate upgrade

migration-check:
	.venv/bin/python -m pymicroservices.app.migrate check

unit:
	.venv/bin/python -m pytest tests/unit -q

integration:
	RAG_RUN_INTEGRATION=1 .venv/bin/python -m pytest tests/integration -q

# Explicit download only; obtain user approval before running.
reranker-model:
	.venv/bin/python -m pymicroservices.app.utilities.retrieval_models
