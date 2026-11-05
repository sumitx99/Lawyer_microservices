# Lexaior backend

FastAPI service behind the Lexaior website, in the `pymicroservices` layout of the RAG service:

- **Sign-in and access** for the Admin and IAM consoles (WorkOS behind our own screens), firm roles +
  matter roles + ethical walls, matters and collections — everything under `/api`.
- **RAG pipeline API**: folder uploads with presigned PUTs (size, type and SHA-256 enforced), per-stage
  status, retrigger, a transactional outbox that wakes the consumers through Kafka, and hybrid retrieval
  (BGE-M3 vectors + full text + legal references, BGE reranker) over pgvector.

A **collection is a folder** of the pipeline (`unstructured_thread` with the collection's id), so every
upload into a collection is processed by the consumers (standardization → markdown/image →
metaclassifier). Access is always the collection's: walls and grants apply to uploads, downloads and search.

```
pymicroservices/app/
  main.py          create_app(): middleware, routers, lifespan (DB, storage, Valkey, outbox)
  controllers/     /api routes (auth, me, admin, matters, collections) + the RAG routes
  services/        collections/ (collections <-> folders, search), firm/ (audit, rate limits, output),
                   threads/, retrigger/, retrieval/, projects/, rag/
  security/        roles, sessions, the route guard, the access engine
  utilities/       settings (secrets_control), async + sync DB, S3 storage, Valkey, Kafka, validation
  models.py        RAG tables          firm_models.py   firm, people, access and audit tables
  alembic/         one migration chain for both (applied on every start)
scripts/           start-api.sh, create_firm.py, seed_dev.py
tests/             unit/ and integration/ (RAG), api/ (sign-in, roles, walls, collection uploads)
```

Document processing lives in the **separate consumers repository**; this service only queues work
for it through the outbox.

The website lives in a separate repo and reaches this API at `/api` through a Cloudflare Worker, so the
browser only ever talks to one domain.

## Routes

| | |
|---|---|
| `/api/...` | Website API. Every route needs an access guard; startup fails otherwise. |
| `POST /api/collections/{id}/uploads` | Declare a file (`filename`, `size_bytes`, `sha256`) → presigned PUT |
| `POST /api/documents/{id}/complete` | Re-check the stored object; first stage WAITING; consumers woken |
| `POST /api/documents/{id}/retry` | Retrigger a failed or stalled stage |
| `POST /api/collections/{id}/search`, `POST /api/matters/{id}/search` | Retrieval over what the caller can read |
| `/threads/unstructured/*`, `/projects/*`, `/retrieval/*`, `/documents*` | RAG routes keyed by `X-User-Id`. **Development and tests only**; not mounted when `ENVIRONMENT` is staging or production. |
| `/health`, `/health/ready` | Liveness (Render's check); readiness (Postgres + storage) |

## Run the whole stack locally

Three repositories, checked out side by side:

```
lexaior/
  lexaior-backend/           this API                      (port 8000)
  lexaior-frontend/          website + consoles, in react/ (port 4173)
  lexaior-kafka-consumers/
    Lawyer_kafka/            Docker infrastructure + the document-processing worker
```

Start Docker Desktop first. Commands are for Windows PowerShell; on macOS/Linux use `.venv/bin/...`
instead of `.venv\Scripts\...`.

**1. Infrastructure and worker** (`lexaior-kafka-consumers/Lawyer_kafka`). Its Docker stack is the
shared infrastructure: Postgres + pgvector `127.0.0.1:5433`, Valkey `6380`, Kafka `9093`,
SeaweedFS (S3) `8333`.

```powershell
python scripts/setup_env.py                     # once: writes .env with random credentials
docker compose up -d --wait --wait-timeout 240
python -m venv .venv
.venv\Scripts\python -m pip install -r pyconsumers/requirements-dev.lock
.venv\Scripts\python -m pyconsumers.start       # the worker; keep this terminal open
```

Indexing and search need the BGE-M3 model in `.cache/huggingface/hub` (about 2.3 GB, see the
`embedding-model` target in its Makefile). The metaclassifier and image stages need Ollama running
natively with the models named in its `.env`.

**2. Backend** (this repository). Create `.env` from `.env.example` and point it at the same
infrastructure, copying the values from the consumers' `.env`:

```ini
ENVIRONMENT=development
DB_SCHEMA=public                   # the consumers read the public schema
# DATABASE_URL=                    # leave unset; POSTGRES_* below are used instead
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5433
POSTGRES_DB=...                    # POSTGRES_*, VALKEY_*, KAFKA_BOOTSTRAP_SERVERS and STORAGE_*:
POSTGRES_USER=...                  # same values as Lawyer_kafka/.env
POSTGRES_PASSWORD=...
VALKEY_HOST=127.0.0.1
VALKEY_PORT=6380
VALKEY_PASSWORD=...
KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9093
STORAGE_PROVIDER=seaweedfs
STORAGE_ENDPOINT_URL=http://127.0.0.1:8333
STORAGE_ACCESS_KEY=...
STORAGE_SECRET_KEY=...
STORAGE_BUCKET=...
EMBEDDING_CACHE_DIR=../lexaior-kafka-consumers/Lawyer_kafka/.cache/huggingface/hub
AUTH_PROVIDER=dev                  # demo sign-in without WorkOS
ADMIN_REQUIRE_MFA=false
```

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r pymicroservices/requirements-dev.lock
.venv\Scripts\python -m pymicroservices.app.migrate upgrade
.venv\Scripts\python -m scripts.seed_dev        # demo firm and one account per role
.venv\Scripts\python -m uvicorn pymicroservices.app.main:create_app --factory --port 8000 --reload --reload-dir pymicroservices
```

Check `http://127.0.0.1:8000/health/ready` and `http://127.0.0.1:8000/docs`. On macOS/Linux,
`make install migrate serve` does the same.

**3. Frontend** (`lexaior-frontend/react`):

```powershell
npm install
npm run dev
```

Open `http://127.0.0.1:4173`; Vite proxies `/api` to port 8000. Sign in with a seeded account,
password `Lexaior-dev-2026`: `admin@morganlaw.test` (Admin console), `maya@` (managing attorney),
`ava@` (attorney), `carlos@` (case manager), `priya@` and `ben@` (paralegals), `felix@` (firm viewer),
all `@morganlaw.test`.

An upload into a collection goes `queued` → `processing` → `ready` while the worker runs; without it,
files stay `queued`. Retrieval loads BGE-M3 and the reranker lazily from `EMBEDDING_CACHE_DIR` (no
downloads at runtime); `torch` and `transformers` are only needed for search.

## Tests

```bash
.venv/Scripts/python -m pytest tests/unit -q          # no services needed
.venv/Scripts/python -m pytest tests/api -q           # Postgres: TEST_DATABASE_URL, else DATABASE_URL from .env
RAG_RUN_INTEGRATION=1 .venv/Scripts/python -m pytest tests/integration -q   # the real RAG infrastructure
```

Run the three suites separately (the API suite sets its own environment). The API suite creates,
truncates and finally drops its own schema (`lexaior_test`, override with `TEST_DB_SCHEMA`, which must
end in `_test`), so it never touches `lexaior`; storage is an in-memory S3 stand-in. Against Supabase
it takes a few minutes. CI runs migrations, unit and API tests against a Postgres + pgvector container.

## Deploy to Render

1. Render → **New → Blueprint** → pick the repo. `render.yaml` creates the `lexaior-api` web service.
2. Fill in the secrets Render asks for:
   - `DATABASE_URL`: Supabase session pooler URL, ending in `?sslmode=require`
   - `WORKOS_API_KEY`, `WORKOS_CLIENT_ID`
   - `STORAGE_*`: an S3-compatible bucket whose CORS allows PUT and GET from the website
   - `VALKEY_HOST`, `VALKEY_PORT`, `VALKEY_PASSWORD`; `KAFKA_BOOTSTRAP_SERVERS` (shared with the consumers)
   - `APP_URL`: the website URL, e.g. `https://lawyer.<subdomain>.workers.dev`
   - `CORS_ORIGINS`: `["https://lawyer.<subdomain>.workers.dev"]`

   `SECRET_KEY` and `PROXY_SECRET` are generated by Render.
3. When it's live, check `https://<service>.onrender.com/health` returns `{"status":"success"}`.
   `/api/...` returns 404 there on purpose: it only answers requests from the Cloudflare Worker.
4. In the website's Cloudflare Worker settings, set `BACKEND_ORIGIN` to the Render URL and
   `PROXY_SECRET` to the value Render generated.
5. In WorkOS → Redirects, set the invitation and password reset URLs to the website's
   `/accept-invite` and `/reset-password`.
6. Create a firm and its first admin (Render → service → **Shell**):

   ```bash
   python -m scripts.create_firm --firm "Firm name" --slug firm --email you@example.com --name "Your Name"
   ```

### Current limits of this deployment
- Without Kafka and the consumers, uploads are stored and checked but stay `queued`; the outbox keeps
  the messages and delivers them once Kafka is reachable.
- Search needs the BGE models in `EMBEDDING_CACHE_DIR` and more memory than Render's free plan has;
  without them search answers 503.
- **Free plan** sleeps after ~15 minutes idle; the first request then takes up to a minute.
