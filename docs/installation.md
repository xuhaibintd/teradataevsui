# First Installation Guide

> **Language:** English | [日本語](installation_ja.md)

This guide starts with an empty Windows AMD64 or Linux x86-64 machine and ends with a running teradataevsui instance. Use the native path for development or a single-host installation. Use Docker Compose when Docker is the deployment boundary.

## 1. Before you begin

Prepare the following:

- Permission to clone `https://github.com/xuhaibintd/teradataevsui.git`.
- Network access to GitHub and the Python package indexes used by `uv`.
- Teradata host, database username and password, UES URL, PAT, and any required PEM file.
- An Unstructured API URL and key only when using `Multi-Format` or `Multi-Format BookRAG`.
- A trusted HTTPS reverse proxy before exposing a production instance outside the local machine.

The supported native runtime is Python 3.11 with `uv` 0.12.10. `uv` can install and manage Python 3.11, so a separate system Python installation is not required. Git and `uv` installation references are available from [Git](https://git-scm.com/install/) and [uv](https://docs.astral.sh/uv/getting-started/installation/).

## 2. Windows PowerShell installation

### 2.1 Install Git, uv, and Python 3.11

Install Git with WinGet or the official Git installer:

```powershell
winget install --id Git.Git -e
```

Install the repository-pinned `uv` version, then open a new PowerShell window if `uv` is not immediately on `PATH`:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/0.12.10/install.ps1 | iex"
uv --version
uv python install 3.11
uv python find 3.11
```

### 2.2 Clone and synchronize the runtime

```powershell
git clone https://github.com/xuhaibintd/teradataevsui.git
Set-Location teradataevsui
git rev-parse --show-toplevel
uv sync --locked --no-dev
uv run --locked --no-sync python -c "import sys; print(sys.version)"
```

Do not replace the locked synchronization with ad hoc `pip install` commands. The command creates `.venv` and installs the exact Windows dependency set from `uv.lock`.

### 2.3 Start a local development instance

```powershell
uv run --locked --no-sync python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

In another PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8010/healthz
```

Open `http://127.0.0.1:8010`. An empty installation redirects to `/setup`; create the first administrator there. There is no initial or default password.

### 2.4 Start a production-style native instance

Generate an in-memory Fernet key and start without `--reload`:

```powershell
$env:EVSUI_CREDENTIAL_KEY = uv run --locked --no-sync python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
$env:EVSUI_ENVIRONMENT = "production"
$env:WEB_CONCURRENCY = "1"
uv run --locked --no-sync python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1
```

The generated key exists only in that PowerShell process. For a persistent service, store the same value in the operating system or deployment secret store and back it up with `data/evsui.db`. Put a trusted HTTPS reverse proxy in front of the loopback listener before external access.

## 3. Linux x86-64 installation

### 3.1 Install Git, curl, uv, and Python 3.11

On Debian or Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
```

On a RHEL-family distribution:

```bash
sudo dnf install -y git curl ca-certificates
```

Install the repository-pinned `uv` version and let it manage Python 3.11:

```bash
curl -LsSf https://astral.sh/uv/0.12.10/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
uv python install 3.11
uv python find 3.11
```

### 3.2 Clone and synchronize the runtime

```bash
git clone https://github.com/xuhaibintd/teradataevsui.git
cd teradataevsui
git rev-parse --show-toplevel
uv sync --locked --no-dev
uv run --locked --no-sync python -c "import sys; print(sys.version)"
```

The command creates `.venv` and installs the exact Linux x86-64 dependency set from `uv.lock`.

### 3.3 Start a local development instance

```bash
uv run --locked --no-sync python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

In another shell:

```bash
curl http://127.0.0.1:8010/healthz
```

Open `http://127.0.0.1:8010`. An empty installation redirects to `/setup`; create the first administrator there. There is no initial or default password.

### 3.4 Start a production-style native instance

```bash
export EVSUI_CREDENTIAL_KEY="$(uv run --locked --no-sync python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export EVSUI_ENVIRONMENT=production
export WEB_CONCURRENCY=1
uv run --locked --no-sync python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1
```

Persist the same key in the host's service secret store and back it up with `data/evsui.db`. Use a service manager for restart policy and a trusted HTTPS reverse proxy for external access.

## 4. Complete first-time browser configuration

1. Open the application URL. With an empty user table, the application displays **Create administrator**.
2. Enter a username, a password of at least eight characters, and the same password again. The password is stored only as an Argon2 hash.
3. Sign in with the new account. `/setup` is disabled immediately after the first user is created.
4. Open **System Configuration → Database Connections** and save the Teradata host, username, password, UES URL, PAT, and PEM content required by the environment.
5. Open **System Configuration → Unstructured IO** and save the API URL and key only when a multi-format mode is required.
6. Return to **Connect & Manage**, select the saved database connection, connect, and run **Refresh management data**.

For unattended initialization only, set both `EVSUI_BOOTSTRAP_ADMIN` and `EVSUI_BOOTSTRAP_PASSWORD` before the first start. Leave both empty to use the browser setup. A partial pair is rejected, and bootstrap values never overwrite an existing user.

## 5. Docker Compose installation

Install Docker Desktop on Windows or Docker Engine with the Compose plugin on Linux. The Compose path does not require a host Python installation.

### 5.1 Prepare `.env` on Windows

```powershell
git clone https://github.com/xuhaibintd/teradataevsui.git
Set-Location teradataevsui
Copy-Item .env.example .env
$bytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$key = [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_')
$rng.Dispose()
$key
notepad .env
```

Copy the printed key into `EVSUI_CREDENTIAL_KEY=`. Leave both bootstrap values empty for browser initialization, or fill both for unattended initialization.

### 5.2 Prepare `.env` on Linux

```bash
git clone https://github.com/xuhaibintd/teradataevsui.git
cd teradataevsui
cp .env.example .env
head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n'; echo
${EDITOR:-vi} .env
```

Copy the printed key into `EVSUI_CREDENTIAL_KEY=`. Leave both bootstrap values empty for browser initialization, or fill both for unattended initialization.

### 5.3 Build, start, verify, and stop

```bash
docker compose config
docker compose up --build -d
docker compose ps
docker compose logs --follow teradataevsui
```

Verify `http://127.0.0.1:8010/healthz`, then open `http://127.0.0.1:8010`. Stop log following with `Ctrl+C`; the container continues running. Stop the service with:

```bash
docker compose down
```

The bind-mounted `data/`, `uploads/`, and `pem_runtime/` directories survive container replacement. Do not delete `data/evsui.db` or its credential key during an upgrade.

## 6. Environment configuration rules

| Variable | Development default | Production rule |
|---|---|---|
| `EVSUI_ENVIRONMENT` | `development` | Set to `production` |
| `WEB_CONCURRENCY` | `1` | Must remain `1` |
| `EVSUI_DATABASE_PATH` | `data/evsui.db` | Place on persistent storage |
| `EVSUI_CREDENTIAL_KEY` | Generated local key is allowed | Supply and back up an explicit Fernet key |
| `EVSUI_CREDENTIAL_KEY_FILE` | DB-adjacent path | Alternative to the direct key; pre-create and protect it |
| `EVSUI_BOOTSTRAP_ADMIN` | Empty | Optional; set together with bootstrap password |
| `EVSUI_BOOTSTRAP_PASSWORD` | Empty | Optional; at least eight characters when set |
| `EVSUI_EXTERNAL_API_ENABLED` | `false` | Enable only when token access is required |
| `EVSUI_API_TOKEN` | Empty | Required when the external API is enabled |

Docker Compose automatically reads `.env`. Native PowerShell and Linux launches do not automatically read `.env`; export the required variables in the service or shell environment. Never commit `.env`, credential keys, passwords, PATs, API keys, PEM files, `data/`, or `uploads/`.

## 7. Update an existing Git checkout

Back up runtime state, confirm that local source changes will not be overwritten, and then update:

```bash
uv run --locked --no-sync python -m app.db backup
git status --short
git pull --ff-only
uv sync --locked --no-dev
uv run --locked --no-sync python -m app.db migrate
```

Restart the single application process after the commands finish. Keep the existing database, credential key, uploads, and PEM runtime directories. See [Operations](operations.md) for backup and recovery details.

## 8. Installation checks and common failures

- `uv sync --locked --no-dev` must finish without changing `uv.lock`.
- `uv run --locked --no-sync python -c "import sys; print(sys.version)"` must report Python 3.11.
- `/healthz` returning `{"status":"ok"}` verifies only the web process, not Teradata or Unstructured connectivity.
- If `/setup` does not appear on a genuinely empty installation, remove legacy login values or bootstrap variables before restart; do not delete an active production database to force setup.
- If production startup reports a missing credential key, set `EVSUI_CREDENTIAL_KEY` or an explicit `EVSUI_CREDENTIAL_KEY_FILE`.
- If startup rejects multiple workers, restore `WEB_CONCURRENCY=1` and `--workers 1`.
- If `docker compose config` reports a missing key, fill `EVSUI_CREDENTIAL_KEY` in `.env`.
- If Teradata or Unstructured access fails after login, verify the corresponding **System Configuration** entry; `/healthz` does not test those services.
