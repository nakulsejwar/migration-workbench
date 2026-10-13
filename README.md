# Agentic Data Migration Planner and Reconciliation Workbench

A verified, auditable data migration platform designed for bounded, mission-critical migrations (`legacy_customers` → `accounts`, max 200 records). 

Built with **FastAPI + Pydantic v2 + SQLAlchemy 2 + SQLite (WAL mode)** on the backend, and a modular **Vanilla JS ES-module frontend** communicating purely over REST.

---

## 1. Verified Architecture & Design

```
frontend/ (index.html, css/, js/{api,util,views,app}.js)  → HTTP →  backend/app/
  api/routes.py        REST endpoints, demo user dependency, error envelope
  services/engine.py   Deterministic migration engine (pure rules, validation, dry run, reconciliation)
  services/workflow.py Authoritative workflow state machine (immutable plans, gates, runs, retry, rollback)
  agent/               OpenRouter agent (tool loop, constrained allowlist, output normalizer)
  models.py database.py audit.py schemas.py config.py errors.py main.py
```

- **Client/Server Separation**: Authoritative state is kept entirely on the server. The frontend uses `localStorage` only to store a non-authoritative demo identity string.
- **Single-Origin Deployment**: FastAPI serves the frontend as static files under `/`, eliminating cross-origin complexity while supporting configurable `CORS_ORIGINS` if hosted separately.
- **Audit & Immutability**: Plan versions and audit logs are append-only. Plan edits spawn new immutable versions with unique content hashes.

---

## 2. Setup & Local Execution

### Prerequisites
- Python 3.11+
- Virtual environment tool (`venv`)

### Installation & Run
```bash
# 1. Navigate to the backend directory
cd backend

# 2. Create and activate a virtual environment
python -m venv my_env
# Windows:
.\my_env\Scripts\activate
# Linux/macOS:
source my_env/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp ../.env.example ../.env    # Edit ../.env to add OPENROUTER_API_KEY if desired

# 5. Run the application
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Once started:
- **Web UI**: [http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- **API Documentation (Swagger)**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **Liveness probe**: `GET /health` (`{"status": "ok"}`)
- **Readiness probe**: `GET /ready` (`{"status": "ready", "database": true, "llm_configured": true}`)

---

## 3. Environment Variables

Configure via `.env` in the project root or backend folder:

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `DATABASE_URL` | `sqlite:///./data/workbench.db` | Path to persistent SQLite database file |
| `OPENROUTER_API_KEY` | *(None)* | API key for OpenRouter AI planning agent |
| `OPENROUTER_PRIMARY_MODEL` | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | Primary tool-calling model |
| `OPENROUTER_FALLBACK_MODELS` | `nvidia/nemotron-3.5-lightning:free,...` | Fallback models for failover |
| `OPENROUTER_TIMEOUT` | `60` | HTTP timeout in seconds for LLM requests |
| `OPENROUTER_MAX_RETRIES` | `2` | Number of provider retries on transient errors |
| `AGENT_MAX_ROUNDS` | `20` | Max autonomous agent tool iterations |
| `DEMO_USERS` | `alice,bob` | Allowed demo user identities for `X-Demo-User` header |
| `ENABLE_TEST_RUNNER` | `1` | Allows running pytest via the in-app UI (disable on production) |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

> **Security Note**: `OPENROUTER_API_KEY` is read only by the backend, redacted in application logs, and never exposed to the frontend or via `/api/config`.

---

## 4. Database Initialization & Persistence

- The database file is located at `backend/data/workbench.db` with SQLite Write-Ahead Logging (`WAL`) enabled.
- On first boot, tables and initial snapshots are automatically initialized:
  - **Source snapshot**: 60 records loaded from `legacy_customers`.
  - **Mock target table**: Pre-seeded with 3 initial records (`ACC-9001`, `CUST-1007`, `CUST-1010`).
- **Persistence Across Restarts**: All plans (v1–v7), approvals, migration runs, target records, quarantine evidence, and audit logs persist across server restarts.

---

## 5. Verification & Testing

Run the full pytest suite in the virtual environment:
```powershell
cd backend
.\my_env\Scripts\python.exe -m pytest -v
```
**Test Results**: **89 passed in ~15.7s** (0 failures, 0 skipped).

### Verified Test Areas:
1. `tests/test_migration_integrity.py`: Phone region normalization, business policy enforcement (`tier`, `marketing_opt_in`), dry-run count invariants, duplicate key detection with prior errors.
2. `tests/test_api.py`: REST routes, approval gates, stale invalidations, concurrency guards, failure simulation, idempotency, retry, rollback, reconciliation.
3. `tests/test_engine.py`: Pure deterministic rule engine, hash generation, schema validation.
4. `tests/test_agent.py`: Agent tool constraints, proposal normalization, schema introspection.
5. `tests/test_gateway.py`: OpenRouter client handling, error propagation, fallback behavior.

---

## 6. Migration Lifecycle & Enforced Invariants

### 1. Count Semantics & Invariants
- **Source Count**: Total records read from the snapshot ($60$).
- **Transformed Count**: Pipeline stage metric — records that completed all transformations prior to target validation ($53$ under approved IN region; $45$ without region).
- **Accepted Count**: Terminal state — records passing transformation, target schema constraints, and business policies ($49$).
- **Quarantined Count**: Terminal state — records rejected with recorded field-level evidence ($11$).
- **Strict Invariant**:
  $$\text{source\_count} = \text{accepted\_count} + \text{quarantined\_count} \quad (60 = 49 + 11)$$
  *Transformed is a pipeline stage metric and is never added to terminal categories.*

### 2. Business Policy & Phone Integrity Safeguards
- **Configurable Phone Region**: No silent US or India defaults. Unqualified national numbers require explicit reviewer-approved region configuration; ambiguous numbers are quarantined. Valid international prefixes (`+` or `00`) are preserved.
- **Explicit Business Policies**: No silent defaults for `tier` or `marketing_opt_in`. Unresolved mandatory policies block approval and execution server-side (`HTTP 409`).
- **Duplicate Key Detection**: Duplicate primary keys (e.g. Row 32 `CUST-1006`) are strictly detected and quarantined even if the record contains other validation or policy errors.

### 3. Server-Enforced Gates
- **Stale Plan Invalidation**: Any change to source data, mapping parameters, or dry-run hashes marks existing approvals and dry runs as `stale`, prohibiting execution.
- **Simulated Outages & Idempotent Retries**: Executions support simulated outage checkpoints (`simulate_failure_after`). Retries resume cleanly without duplicate inserts.
- **Safe Rollbacks**: Rollback removes only records inserted by the specific run, leaving seed data and records from unrelated runs intact.
- **Six Reconciliation Invariants**: Reconciles source vs. terminal counts, accepted vs. target records, inserted ledger, control totals of `balance_minor`, quarantine evidence completeness, and untouched pre-existing records.

---

## 7. Known Limitations

1. **SQLite Concurrency**: SQLite serializes writes (`BEGIN IMMEDIATE`). Designed for single-worker or team use; high-concurrency multi-tenant scaling requires PostgreSQL.
2. **Demo Identity Header**: The `X-Demo-User` header provides audit tracking for the demo interface but does not replace enterprise SSO/OAuth. Production deployment should place the service behind an authenticating reverse proxy.
3. **Source Snapshot Size**: Optimized and bounded for migration batches up to 200 records per plan.

---

## 8. Deployment Instructions

### Docker Container Deployment
```bash
# Build and run with Docker Compose
docker compose up --build
```
The Docker container mounts a persistent volume for `/data/workbench.db` and runs the unified FastAPI + static frontend service.

### Cloud Deployment (Render / Fly.io / Cloud Run)
1. Set environment variables (`DATABASE_URL`, `OPENROUTER_API_KEY`, `DEMO_USERS`, `ENABLE_TEST_RUNNER=0`).
2. Attach a persistent volume for the SQLite database directory (e.g. `/data`).
3. Deploy the container using the included [Dockerfile](file:///c:/Users/Nakul/Downloads/migration-workbench/migration-workbench/Dockerfile).
