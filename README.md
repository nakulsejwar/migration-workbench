# Agentic Data Migration Planner and Reconciliation Workbench

A verified, auditable data migration platform designed for bounded, mission-critical migrations (`legacy_customers` → `accounts`, maximum 200 records per plan).

Built with **FastAPI, Pydantic v2, SQLAlchemy 2, and SQLite (WAL mode)** on the backend, with a modular **Vanilla JavaScript ES-module frontend** communicating over REST.

---

## 1. Architecture and Design

```text
frontend/
  index.html
  css/
  js/{api,util,views,app}.js
          │ HTTP / REST
          ▼
backend/app/
  api/routes.py          REST endpoints, demo identity dependency, error envelope
  services/engine.py     Deterministic migration engine, validation, dry run, reconciliation
  services/workflow.py   Workflow state machine, immutable plans, approval gates, runs, retry, rollback
  agent/                 OpenRouter agent, constrained tool allowlist, output normalizer
  models.py
  database.py
  audit.py
  schemas.py
  config.py
  errors.py
  main.py
```

- **Client/server separation:** Authoritative workflow state is maintained by the server. The frontend uses `localStorage` only for a non-authoritative demo identity string.
- **Single-origin deployment:** FastAPI serves the frontend as static files under `/`. `CORS_ORIGINS` can be configured when hosting the frontend separately.
- **Audit and plan immutability:** Plan versions and audit logs are append-only. Editing a plan creates a new immutable version with a content hash.
- **Bounded execution:** The migration workflow is designed for batches of up to 200 records per plan.

---

## 2. Setup and Local Execution

### Prerequisites

- Python 3.11+
- `venv`
- An OpenRouter API key for AI-assisted planning

### Install and run

Run these commands from the repository root.

```powershell
# 1. Enter the backend directory
cd backend

# 2. Create and activate a virtual environment
python -m venv my_env
.\my_env\Scripts\Activate.ps1

# 3. Install dependencies
python -m pip install -r requirements.txt

# 4. Create the project-root environment file if it does not exist
Copy-Item ..\.env.example ..\.env

# 5. Edit ..\.env and configure the required provider credentials/settings

# 6. Start the application
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

For Linux/macOS, activate the environment with:

```bash
source my_env/bin/activate
```

Once the server starts:

- **Web UI:** <http://127.0.0.1:8000/>
- **API documentation:** <http://127.0.0.1:8000/docs>
- **Liveness probe:** `GET /health`
- **Readiness probe:** `GET /ready`

The readiness response reports database readiness and whether an LLM API key is configured. A configured key does not guarantee that the upstream provider is available or that its rate limits have not been reached.

---

## 3. Environment Configuration

The application reads environment variables from the process environment and `.env` files. Keep API keys private; do not commit your real `.env` file.

| Variable | Default / example | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/workbench.db` | SQLite database URL/path |
| `OPENROUTER_API_KEY` | Empty | API key for OpenRouter AI planning |
| `OPENROUTER_PRIMARY_MODEL` | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | Primary OpenRouter model |
| `OPENROUTER_FALLBACK_MODELS` | `nvidia/nemotron-3.5-lightning:free,nvidia/nemotron-3-super-120b-a12b:free` | Comma-separated OpenRouter fallback models |
| `OPENROUTER_ENABLE_FALLBACKS` | `1` | Enables configured OpenRouter fallback models when supported by the application settings |
| `OPENROUTER_TIMEOUT` | `60` | OpenRouter request timeout, in seconds |
| `OPENROUTER_MAX_RETRIES` | `1` | Retry count configured by the current settings implementation |
| `AGENT_MAX_ROUNDS` | `20` | Maximum agent tool-loop iterations |
| `DEMO_USERS` | `alice,bob` | Allowed demo identities for the `X-Demo-User` header |
| `ENABLE_TEST_RUNNER` | `1` | Enables the in-app test runner; disable in production |
| `LOG_LEVEL` | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `NVIDIA_API_KEY` | Empty unless NIM fallback is configured | API key for direct NVIDIA NIM fallback |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | NVIDIA NIM API base URL |
| `NVIDIA_MODEL` | `deepseek-ai/deepseek-v4.1-flash` | NVIDIA NIM fallback model |

**Provider note:** OpenRouter model fallbacks and direct NVIDIA NIM fallback are separate paths. Configure NVIDIA credentials only if the installed code version supports direct NIM fallback. A fallback attempt can still fail because of provider availability, rate limits, network conditions, or timeouts.

**Security note:** `OPENROUTER_API_KEY` and `NVIDIA_API_KEY` are backend secrets. Do not expose them in frontend code, API responses, screenshots, or committed files.

---

## 4. Database Initialization and Persistence

- The default SQLite database path is relative to the backend working directory: `backend/data/workbench.db`.
- SQLite Write-Ahead Logging (WAL) is enabled.
- On first boot, the application initializes its tables and seed data:
  - **Source snapshot:** 60 records from `legacy_customers`.
  - **Mock target table:** seeded with initial records such as `ACC-9001`, `CUST-1007`, and `CUST-1010`.
- Plans, approvals, migration runs, target records, quarantine evidence, and audit events are persisted according to the application’s database implementation.

Database state and seed behavior can depend on the configured `DATABASE_URL` and whether an existing database file is already present.

---

## 5. Verification and Testing

Run the backend test suite from the `backend` directory:

```powershell
python -m pytest -v
```

Or, with the project virtual environment explicitly selected:

```powershell
.\my_env\Scripts\python.exe -m pytest -v
```

### Latest verified result

**14 passed in 3.32 seconds** on Python 3.11, as reported by the local test run on October 13, 2026.

The test suite currently contains:

- `tests/test_engine.py` — 8 tests
- `tests/test_workflow_api.py` — 6 tests

### Verified test coverage

- Stable serialization and deterministic hashing
- Reference mapping validation
- Rejection of unknown transformation rules and missing required mappings
- Deterministic dry runs and source/terminal-count invariants
- Duplicate source-key quarantine
- Oversized-input rejection
- Protection against overwriting conflicting target records
- Rollback removes only rows owned by the relevant run
- Health and readiness endpoints
- Demo identity requirement for mutating endpoints
- Execution blocked without approval
- Dry runs do not mutate the target and are repeatable
- Failed-run retry, reconciliation, and rollback workflow
- Rejection of a second execution for the same approved plan

These results describe the automated tests only. They do **not** independently verify live AI-provider availability, production deployment, or every possible browser interaction. Re-run the suite after changing code or dependencies.

---

## 6. Migration Lifecycle and Invariants

### Count semantics

For the seeded 60-record source dataset, the documented reference workflow is:

- **Source count:** 60 records read from the snapshot.
- **Transformed count:** a pipeline-stage metric; the reference run reports 53 under an approved India phone-region choice.
- **Accepted count:** 49 records that pass transformation, target-schema constraints, and business policies.
- **Quarantined count:** 11 rejected records with recorded evidence.

The terminal-count invariant is:

`source_count = accepted_count + quarantined_count`

For the reference run: `60 = 49 + 11`.

The transformed count is a pipeline-stage metric and is **not** added to the terminal categories. Counts depend on the approved plan and input data; they should be read from the actual run rather than assumed for every configuration.

### Business policy and phone integrity

- **Explicit phone region:** National phone numbers require an explicit reviewer-approved region where applicable. Ambiguous or invalid numbers should be quarantined rather than silently interpreted.
- **Explicit business policies:** Values such as `tier` and `marketing_opt_in` should be resolved explicitly where required. Unresolved mandatory policies should block approval or execution.
- **Duplicate key detection:** Duplicate source keys should be identified and quarantined, including when a record has additional validation errors.

### Server-enforced workflow gates

- **Approval gate:** Execution requires approval of the applicable plan version.
- **Stale-plan protection:** Changes to source data or plan/dry-run state should invalidate stale approvals or dry-run results where implemented by the workflow.
- **Retry safety:** Failed runs can be retried without duplicating inserts when the checkpoint/idempotency path applies.
- **Safe rollback:** Rollback should remove only records owned by the specific run, preserving seed data and unrelated records.
- **Reconciliation:** Reconciliation checks source/terminal counts and execution outcomes against the recorded migration state.

---

## 7. Known Limitations

1. **SQLite concurrency:** SQLite serializes writes. The application is intended for a bounded workload and single-worker or small-team use. Higher-concurrency multi-tenant workloads may require PostgreSQL and additional operational design.
2. **Demo identity:** `X-Demo-User` is for demo/audit attribution and is not enterprise authentication. Production deployment should use a real authentication mechanism or an authenticated reverse proxy.
3. **Bounded batch size:** The workflow is designed for migrations of up to 200 records per plan.
4. **External AI dependencies:** AI planning depends on upstream provider availability, configured credentials, model/tool-call compatibility, network conditions, and rate limits. Deterministic migration validation and execution rules should remain authoritative over AI-generated proposals.
5. **Provider fallback:** A fallback is not guaranteed to succeed. Verify each configured provider/model in the deployment environment.

---

## 8. Deployment

### Docker Compose

If Docker Compose and the required files are present in the repository:

```bash
docker compose up --build
```

Ensure the configured database path is backed by a persistent volume and matches the container’s database configuration.

### Cloud deployment

For a platform such as Render, Fly.io, or Cloud Run:

1. Configure `DATABASE_URL`, provider credentials, demo-user settings, and other required environment variables.
2. Disable the in-app test runner in production with `ENABLE_TEST_RUNNER=0`.
3. Configure persistent storage if using SQLite; ephemeral filesystems can lose database state during redeployments.
4. Configure real authentication before exposing the application to untrusted users.
5. Verify health/readiness endpoints, provider access, logs, and persistence after deployment.

Use the repository-relative `Dockerfile` and deployment configuration rather than a machine-specific local file path.
