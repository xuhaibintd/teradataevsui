# teradataevsui Operations

> **Language:** English | [日本語](operations_ja.md)

For a fresh Windows, Linux, or Docker Compose deployment, start with the [First Installation Guide](installation.md). This document covers ongoing operation and recovery after installation.

## Local development

teradataevsui uses one standard Python process instead of project-specific lifecycle
wrappers or a separately managed job service.

```powershell
uv sync --locked
uv run --locked --no-sync python -m app.db migrate
uv run --locked --no-sync python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

The application starts its background job runner automatically. Document parsing,
CSV generation/loading, and Vector Store creation execute one at a time without
blocking the HTTP event loop. Stop the application with `Ctrl+C`; shutdown waits for
the current job to finish and does not claim another job. A database-scoped lock
rejects a second application process using the same SQLite database.

For complete CI-equivalent verification, including dependency policy, browser
actions, and package-content checks, follow [testing.md](testing.md). These are
the quick backend checks:

```powershell
uv run --locked --no-sync ruff check app tests scripts
uv run --locked --no-sync python -m compileall -q app scripts
uv run --locked --no-sync python -m unittest discover -s tests -q
```

## Database lifecycle

`data/evsui.db` is runtime state and is intentionally ignored by Git. A fresh checkout creates it from numbered migrations. Existing installations are upgraded idempotently at startup or explicitly with:

```powershell
.\.venv\Scripts\python.exe -m app.db status
.\.venv\Scripts\python.exe -m app.db migrate
```

Current schema version 9 contains:

| Table | Purpose |
|---|---|
| `schema_versions` | Applied migration history |
| `users`, `sessions`, `permissions`, `audit_logs` | Identity, access, opaque server sessions, and audit events |
| `system_connection_profiles` | Reusable Teradata profiles with encrypted password, PAT, and PEM |
| `external_service_configs` | Shared service endpoints and encrypted API keys, currently Unstructured IO |
| `jobs` | Durable queued/running/completed state, progress, and transient encrypted job secrets |
| `artifacts` | Tracked file metadata and retention state |
| `connection_configs`, `system_connection_config` | Compatibility tables retained for migration from older releases |

Back up a live SQLite database with its online backup API:

```powershell
.\.venv\Scripts\python.exe -m app.db backup
```

The default destination is `data/backups/`. Back up the credential key together with the database. Without the same Fernet key, encrypted database passwords, PATs, PEM contents, and external API keys cannot be recovered.

## Artifacts and persistent maintenance jobs

Inventory is read-only. Cleanup is also a dry run unless both the environment flag and `--apply` are present.

```powershell
.\.venv\Scripts\python.exe -m app.ops inventory
.\.venv\Scripts\python.exe -m app.ops cleanup-artifacts
$env:EVSUI_ARTIFACT_CLEANUP_ENABLED = "true"
.\.venv\Scripts\python.exe -m app.ops cleanup-artifacts --apply
```

The background runner executes one job at a time because the Teradata SDK context is process-global. It records a heartbeat every 30 seconds even while an SDK call is blocking. On startup, jobs whose heartbeat is older than `EVSUI_JOB_STALE_SECONDS` are returned to the queue. Queued jobs can be cancelled from the UI; already-running external operations are not force-killed.

Vector Store readiness is polled every `EVS_VECTORSTORE_READY_POLL_SECONDS` (default 5 seconds) for at most `EVS_VECTORSTORE_READY_TIMEOUT_SECONDS` (default 7200 seconds). A timeout marks the job failed but does not cancel remote work that Teradata has already accepted, so verify remote status before retrying.

Only tracked, expired files below `uploads/` are eligible. Existing untracked files are never automatically registered or deleted.

## Production requirements

- Set `EVSUI_ENVIRONMENT=production`.
- Set `WEB_CONCURRENCY=1`.
- Provide `EVSUI_CREDENTIAL_KEY` or an explicit, pre-created `EVSUI_CREDENTIAL_KEY_FILE`.
- Mount persistent storage for `data/`, `uploads/`, and `pem_runtime/`.
- Terminate HTTPS at a trusted reverse proxy and preserve `Host`, `Origin`, and `X-Forwarded-Proto` correctly.
- Enable the external API only when needed with `EVSUI_EXTERNAL_API_ENABLED=true` and a strong `EVSUI_API_TOKEN`.
- Schedule online database backups and dry-run artifact inventory before enabling cleanup.

`compose.yaml` starts one application service. Its HTTP server and background job
runner share the same SQLite repository, credential vault, artifact lifecycle, and
Teradata runtime lock. The Compose defaults leave both bootstrap variables empty so a fresh deployment opens the one-time browser setup. Set both variables explicitly only for unattended initialization.
